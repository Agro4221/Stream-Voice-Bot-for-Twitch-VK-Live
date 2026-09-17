from __future__ import annotations

import re
from num2words import num2words

DIGIT_WORDS = {
    "0": "ноль", "1": "один", "2": "два", "3": "три", "4": "четыре",
    "5": "пять", "6": "шесть", "7": "семь", "8": "восемь", "9": "девять",
}


def _num_words(n: str) -> str:
    try:
        return num2words(int(n), lang="ru")
    except Exception:
        return " ".join(DIGIT_WORDS.get(ch, ch) for ch in n)


def normalize_for_tts(text: str) -> str:
    """Turn common stream text into speech-friendly Russian text.

    This is deliberately conservative: URLs, emoji and decorative symbols are
    simplified, while ordinary words/punctuation remain intact.
    """
    if not text:
        return ""

    text = text.replace("ё", "ё")
    text = re.sub(r"https?://\S+|www\.\S+", " ссылка ", text, flags=re.I)
    text = re.sub(r"\bdiscord(?:\.gg/\S+)?\b", "дискорд", text, flags=re.I)

    # Percentages: 25% -> двадцать пять процентов
    def pct(m):
        return f"{_num_words(m.group(1))} процентов"
    text = re.sub(r"(?<!\w)(\d{1,9})\s*%", pct, text)

    # Decimal comma/dot: 1,6 -> одна целая шесть десятых
    def decimal(m):
        whole, frac = m.group(1), m.group(2)
        try:
            denom = {1: "десятых", 2: "сотых", 3: "тысячных"}.get(len(frac), "десятичных")
            return f"{_num_words(whole)} целых {_num_words(frac)} {denom}"
        except Exception:
            return f"{_num_words(whole)} запятая {' '.join(DIGIT_WORDS.get(c, c) for c in frac)}"
    text = re.sub(r"(?<!\w)(\d{1,9})[\.,](\d{1,4})(?!\w)", decimal, text)

    # Time: 12:30 -> двенадцать тридцать
    def tm(m):
        return f"{_num_words(m.group(1))} {_num_words(m.group(2))}"
    text = re.sub(r"(?<!\w)(\d{1,2}):(\d{2})(?!\w)", tm, text)

    # Long digit runs are usually IDs, phone numbers or codes: read digits
    # individually instead of as a giant integer.
    def long_digits(m):
        digits = m.group(1)
        if len(digits) >= 6:
            return " ".join(DIGIT_WORDS.get(c, c) for c in digits)
        return _num_words(digits)
    text = re.sub(r"(?<!\w)(\d{1,12})(?!\w)", long_digits, text)

    # Symbols that are meaningful in chat.
    replacements = {
        "@": " собака ",
        "#": " хэштег ",
        "&": " и ",
        "+": " плюс ",
        "=": " равно ",
        "₽": " рублей ",
        "$": " долларов ",
        "€": " евро ",
    }
    for a, b in replacements.items():
        text = text.replace(a, b)

    # Drop decorative/control characters, preserving Cyrillic/Latin, digits and
    # normal punctuation. Emojis are intentionally omitted from the spoken text.
    text = re.sub(r"[^\w\s\u0400-\u04FF.,!?;:()'\"%\-]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text
