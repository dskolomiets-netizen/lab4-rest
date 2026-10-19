# Приведение текста к ASCII: весь вывод программы должен быть ASCII.
# Перенесено из ЛР3 (spotexport/text.py) без изменений логики.

import unicodedata

# Латиница для букв а..я (U+0430..U+044F) по порядку:
# а б в г д е ж з и й к л м н о п р с т у ф х ц ч ш щ ъ ы ь э ю я
_LATIN = [
    "a", "b", "v", "g", "d", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p",
    "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya",
]
CYRILLIC = {chr(0x0430 + i): latin for i, latin in enumerate(_LATIN)}
# ё і ї є ґ
CYRILLIC |= {"ё": "e", "і": "i", "ї": "yi", "є": "ye", "ґ": "g"}

# Типографские знаки из названий треков: дефисы и тире, кавычки, многоточие
PUNCTUATION = {
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "«": '"', "»": '"', "…": "...", " ": " ",
}


def to_ascii(text: str) -> str:
    # Кириллицу транслитерировать, у латиницы снять диакритику (Ÿ -> Y),
    # всё остальное (иероглифы и т.п.) записать как \uXXXX
    out: list[str] = []
    for ch in text:
        if ch.isascii():
            out.append(ch)
        elif ch in PUNCTUATION:
            out.append(PUNCTUATION[ch])
        elif ch.lower() in CYRILLIC:
            latin = CYRILLIC[ch.lower()]
            out.append(latin if ch.islower() else latin.capitalize())
        else:
            # NFKD раскладывает "Ÿ" на "Y" + диакритический знак
            base = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
            out.append(base or ch.encode("ascii", "backslashreplace").decode())
    return "".join(out)
