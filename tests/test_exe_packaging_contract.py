import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExePackagingContractTests(unittest.TestCase):
    def read(self, path):
        return (ROOT / path).read_text(encoding="utf-8")

    def test_exe_entry_is_self_hosted(self):
        src = self.read("scripts/exe_entry.py")
        self.assertIn('getattr(sys, "frozen", False)', src)
        self.assertIn("Path(sys.executable).resolve().parent", src)
        self.assertIn('host="127.0.0.1"', src)
        self.assertIn('port=8787', src)
        self.assertIn('webbrowser.open(URL)', src)
        self.assertNotIn("subprocess.Popen", src)

    def test_spec_contains_runtime_assets(self):
        src = self.read("packaging/StreamVoiceBot.spec")
        self.assertIn('PROJECT_ROOT = Path.cwd().resolve()', src)
        self.assertIn('"models" / "v5_ru.pt"', src)
        self.assertIn('"stream_voice_bot" / "web"', src)
        self.assertIn('"stream_voice_bot" / "vk_bridge"', src)
        self.assertIn('".runtime" / "node"', src)
        self.assertIn('["scripts" / "exe_entry.py"]', src)
        self.assertNotIn("StreamVoiceBotCore", src)

    def test_build_script_has_one_user_facing_exe(self):
        src = self.read("scripts/build_exe.ps1")
        self.assertIn('"StreamVoiceBot"', src)
        self.assertIn("BUILD SUCCESS", src)
        self.assertIn("Copy-Item $BuiltBundle $FinalStage -Recurse -Force", src)
        self.assertNotIn("StreamVoiceBotCore.exe", src)


if __name__ == "__main__":
    unittest.main()
