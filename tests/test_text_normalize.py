import unittest

from stream_voice_bot.text_normalize import normalize_for_tts


class TTSTextNormalizeTests(unittest.TestCase):
    def test_stream_game_abbreviations_use_phonetic_pronunciation(self):
        source = (
            "А чё они не могут одновременно и хг держать в игре "
            "и лсс,на постоянной основе?"
        )
        result = normalize_for_tts(source)
        self.assertIn("хаур гласс", result)
        self.assertIn("ласт шип стэндинг", result)
        self.assertNotIn("хг", result.lower())
        self.assertNotIn("лсс", result.lower())

    def test_decimal_numbers_have_correct_feminine_agreement(self):
        self.assertIn("одна целых", normalize_for_tts("1,6"))
        self.assertIn("две целых", normalize_for_tts("2,5"))

    def test_percentages_and_long_digit_runs_are_speech_friendly(self):
        self.assertEqual(normalize_for_tts("25%"), "двадцать пять процентов")
        self.assertEqual(
            normalize_for_tts("ID 123456"),
            "ID один два три четыре пять шесть",
        )


if __name__ == "__main__":
    unittest.main()
