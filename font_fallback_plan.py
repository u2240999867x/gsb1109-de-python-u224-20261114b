"""离线网页存档的字体取用计划。

给定：
  * FONT_CSS —— 字体清单（@font-face 规则，按文档次序，含字重与 unicode-range）
  * GLYPHS   —— 每份字体文件实际包含的字形清单（unicode-range 声明覆盖 ≠ 真有字形）
  * REQUEST  —— 待展示文本及其字重

规则：
  1. 只考虑 font-family 与 font-weight 都匹配的 @font-face，按文档次序尝试；
  2. unicode-range 覆盖只是“候选”，字体实际字形清单里真有该字符才算命中；
  3. 变体选择符（U+FE00–FE0F、U+E0100–E01EF）与前一字构成不可拆分的整体请求，
     同一份字体必须同时握有基字与选择符，不允许跨两份字体拼凑命中；
  4. 没有任何字体命中时，该字符（或整组请求）只能回退到系统字体。
"""

import tinycss2

# ---------------------------------------------------------------- 已给数据

FONT_CSS = """
@font-face {
  font-family: "Archive Sans";
  font-weight: 400;
  src: url("archive-latin-400.woff2");
  unicode-range: U+0000-00FF;
}
@font-face {
  font-family: "Archive Sans";
  font-weight: 400;
  src: url("archive-latin-ext-400.woff2");
  unicode-range: U+0000-024F;            /* 与上一份重叠，次序靠后 */
}
@font-face {
  font-family: "Archive Sans";
  font-weight: 400;
  src: url("archive-symbols-400.woff2");
  unicode-range: U+2000-27BF;
}
@font-face {
  font-family: "Archive Sans";
  font-weight: 400;
  src: url("archive-emoji-400.woff2");
  unicode-range: U+2000-27BF, U+FE00-FE0F, U+1F300-1FAFF;
}
@font-face {
  font-family: "Archive Sans";
  font-weight: 400;
  src: url("archive-cjk-400.woff2");
  unicode-range: U+4E00-9FFF;
}
@font-face {
  font-family: "Archive Sans";
  font-weight: 700;                      /* 字重不匹配，本次请求用不到 */
  src: url("archive-latin-700.woff2");
  unicode-range: U+0000-00FF, U+4E00-9FFF;
}
"""

# 每份字体实际字形清单（字符级别）
GLYPHS = {
    "archive-latin-400.woff2": set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ®"),
    "archive-latin-ext-400.woff2": set("éèêàç"),
    "archive-symbols-400.woff2": {"™", "❤"},          # ™、❤（无 VS16）
    "archive-emoji-400.woff2": {"❤", "️", "\U0001f389"},  # ❤+VS16、🎉
    "archive-cjk-400.woff2": set("雪花饼"),
    "archive-latin-700.woff2": set("abcXYZ 雪"),
}

REQUEST = {
    "family": "Archive Sans",
    "weight": 400,
    "text": "雪花饼™ Café ❤️ 龘 🎉🚀",
}

# ---------------------------------------------------------------- 解析清单

def extract_url(tokens):
    """从 src 描述符里取第一个 URL（兼容 url(x) 与 url("x") 两种写法）。"""
    for token in tokens:
        if token.type == "url":
            return token.value
        if token.type == "function" and token.lower_name == "url":
            for arg in token.arguments:
                if arg.type == "string":
                    return arg.value
    return None

def parse_font_faces(css):
    """用 tinycss2 解析 @font-face，保留文档次序。"""
    faces = []
    rules = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
    for rule in rules:
        if rule.type != "at-rule" or rule.lower_at_keyword != "font-face":
            continue
        face = {"order": len(faces), "family": None, "weight": 400,
                "src": None, "ranges": []}
        for decl in tinycss2.parse_declaration_list(
                rule.content, skip_comments=True, skip_whitespace=True):
            if decl.type != "declaration":
                continue
            name = decl.lower_name
            if name == "font-family":
                face["family"] = tinycss2.serialize(decl.value).strip().strip("\"'")
            elif name == "font-weight":
                token = next(t for t in decl.value
                             if t.type in ("number", "ident"))
                if token.type == "number":
                    face["weight"] = int(token.value)
                else:  # normal / bold
                    face["weight"] = {"normal": 400, "bold": 700}[token.lower_value]
            elif name == "src":
                face["src"] = extract_url(decl.value)
            elif name == "unicode-range":
                face["ranges"] = [(t.start, t.end) for t in decl.value
                                  if t.type == "unicode-range"]
        faces.append(face)
    return faces

# ---------------------------------------------------------------- 匹配逻辑

def is_variation_selector(char):
    cp = ord(char)
    return 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF

def split_clusters(text):
    """把 基字+变体选择符 合成一个不可拆分的请求单元。"""
    clusters = []
    for char in text:
        if is_variation_selector(char) and clusters:
            clusters[-1] += char
        else:
            clusters.append(char)
    return clusters

def range_covers(face, char):
    cp = ord(char)
    return any(start <= cp <= end for start, end in face["ranges"])

def match_cluster(cluster, faces, glyphs, family, weight):
    """返回 (face, note)；无命中时 face 为 None。"""
    base = cluster[0]
    skipped = []
    for face in faces:
        if face["family"] != family or face["weight"] != weight:
            continue
        if not range_covers(face, base):
            continue
        have = glyphs.get(face["src"], set())
        if all(char in have for char in cluster):
            note = ""
            if skipped:
                note = "（跳过 %s）" % "、".join(skipped)
            return face, note
        missing = [f"U+{ord(c):04X}" for c in cluster if c not in have]
        skipped.append(f"{face['src']} 范围覆盖但缺字形 {'+'.join(missing)}")
    return None, "；".join(skipped)

# ---------------------------------------------------------------- 输出计划

def main():
    faces = parse_font_faces(FONT_CSS)
    family, weight, text = REQUEST["family"], REQUEST["weight"], REQUEST["text"]

    print(f"请求：font-family {family!r}，font-weight {weight}")
    print(f"文本：{text}\n")

    used, fallback = [], []
    for cluster in split_clusters(text):
        label = " + ".join(f"U+{ord(c):04X}" for c in cluster)
        shown = cluster if cluster.strip() else "␣"
        face, note = match_cluster(cluster, faces, GLYPHS, family, weight)
        if face:
            used.append(face["src"])
            print(f"  {shown!r:8} [{label}] -> {face['src']} {note}")
        else:
            fallback.append(cluster)
            detail = f"（{note}）" if note else "（无任何字体声明覆盖）"
            print(f"  {shown!r:8} [{label}] -> 回退系统字体 {detail}")

    print("\n需取用的字体文件：")
    for src in dict.fromkeys(used):
        print(f"  - {src}")
    print("只能回退的字符：")
    if fallback:
        for cluster in fallback:
            print(f"  - {cluster!r} ({' + '.join(f'U+{ord(c):04X}' for c in cluster)})")
    else:
        print("  （无）")

if __name__ == "__main__":
    main()
