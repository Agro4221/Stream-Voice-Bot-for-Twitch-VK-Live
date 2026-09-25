from __future__ import annotations

import re
import unicodedata

from num2words import num2words


DIGIT_WORDS = {
    "0": "ноль", "1": "один", "2": "два", "3": "три", "4": "четыре",
    "5": "пять", "6": "шесть", "7": "семь", "8": "восемь", "9": "девять",
}

# Common stream/game abbreviations. Keep the pronunciation in Cyrillic:
# the bundled v5_ru model is a Russian TTS model and should not decode
# Latin abbreviations character-by-character.
TTS_LEXICON = (
    (re.compile(r"(?<!\w)(?:хг|hg)(?!\w)", re.IGNORECASE), "хаур гласс"),
    (re.compile(r"(?<!\w)(?:лсс|lss)(?!\w)", re.IGNORECASE), "ласт шип стэндинг"),
)

# Explicit stress overrides for stream-specific words can be added here.
# The combining acute accent is preserved into Silero save_wav().
TTS_STRESS = {}


def _num_words(n: str) -> str:
    try:
        return num2words(int(n), lang="ru")
    except Exception:
        return " ".join(DIGIT_WORDS.get(ch, ch) for ch in n)


def _decimal_words(whole: str, frac: str) -> str:
    try:
        whole_n = int(whole)
        whole_word = {1: "одна", 2: "две"}.get(whole_n, _num_words(whole))
        whole_adjective = "целая" if whole_n == 1 else "целых"
        denom = {1: "десятых", 2: "сотых", 3: "тысячных"}.get(len(frac), "десятичных")
        return f"{whole_word} {whole_adjective} {_num_words(frac)} {denom}"
    except Exception:
        return f"{_num_words(whole)} запятая {" ".join(DIGIT_WORDS.get(c, c) for c in frac)}"


def _apply_tts_lexicon(text: str) -> str:
    for pattern, replacement in TTS_LEXICON:
        text = pattern.sub(replacement, text)
    for source, replacement in TTS_STRESS.items():
        text = re.sub(rf"(?<!\w){re.escape(source)}(?!\w)", replacement, text, flags=re.IGNORECASE)
    return text


def normalize_for_tts(text: str) -> str:
    """Turn stream text into speech-friendly Russian text before Silero."""
    if not text:
        return ""

    text = unicodedata.normalize("NFC", text)
    text = text.replace("\u00a0", " ")
    text = text.replace("’", "'").replace("“", '"').replace("”", '"').replace("–", "-").replace("—", " - ")
    text = re.sub(r"https?://\S+|www\.\S+", " ссылка ", text, flags=re.I)
    text = re.sub(r"\bdiscord(?:\.gg/\S+)?\b", "дискорд", text, flags=re.I)

    # Expand known slang before number processing.
    text = _apply_tts_lexicon(text)

    def pct(m):
        return f"{_num_words(m.group(1))} процентов"

    text = re.sub(r"(?<!\w)(\d{1,9})\s*%", pct, text)
    text = re.sub(r"(?<!\w)(\d{1,9})[\.,](\d{1,4})(?!\w)", lambda m: _decimal_words(m.group(1), m.group(2)), text)

    def tm(m):
        return f"{_num_words(m.group(1))} {_num_words(m.group(2))}"

    text = re.sub(r"(?<!\w)(\d{1,2}):(\d{2})(?!\w)", tm, text)

    def long_digits(m):
        digits = m.group(1)
        if len(digits) >= 6:
            return " ".join(DIGIT_WORDS.get(c, c) for c in digits)
        return _num_words(digits)

    text = re.sub(r"(?<!\w)(\d{1,12})(?!\w)", long_digits, text)

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

    text = re.sub(r"[^\w\s\u0400-\u04FF\u0300-\u036F.,!?;:()'\"%\-]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text