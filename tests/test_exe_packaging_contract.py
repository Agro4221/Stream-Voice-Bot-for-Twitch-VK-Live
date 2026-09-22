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
        self.assertIn("def create_desktop_shortcut_once", src)
        self.assertIn('".desktop_shortcut_created"', src)
        self.assertIn("subprocess.run(", src)
        self.assertIn("powershell.exe", src)
        self.assertIn("CREATE_NO_WINDOW", src)
        self.assertNotIn("subprocess.Popen", src)
        self.assertNotIn("Create_Desktop_Shortcut.cmd", src)

    def test_spec_contains_runtime_assets_and_safe_onedir_layout(self):
        src = self.read("packaging/StreamVoiceBot.spec")
        self.assertIn('PROJECT_ROOT = Path.cwd().resolve()', src)
        self.assertIn('"models" / "v5_ru.pt"', src)
        self.assertIn('"stream_voice_bot" / "web"', src)
        self.assertIn('"stream_voice_bot" / "vk_bridge"', src)
        self.assertIn('str(PROJECT_ROOT / ".runtime" / "node" / "node.exe")', src)
        self.assertIn("binaries = [", src)
        self.assertNotIn('content-factory-favicon.png",\n        "packaging"', src)
        self.assertIn('[str(PROJECT_ROOT / "scripts" / "exe_entry.py")]', src)
        self.assertIn("exclude_binaries=True", src)
        self.assertIn('contents_directory="_internal"', src)
        self.assertIn("collect_dynamic_libs(package)", src)
        self.assertIn('"torch",', src)
        self.assertNotIn("collect_all", src)
        self.assertNotIn("StreamVoiceBotCore", src)

    def test_stt_ui_exposes_auto_gpu_and_cpu_modes(self):
        src = self.read("stream_voice_bot/web/index.html")
        self.assertIn('value="auto">Авто (CUDA → CPU)', src)
        self.assertIn('value="cuda">GPU (NVIDIA CUDA)', src)
        self.assertIn('value="cpu">CPU int8', src)
        self.assertIn("cuBLAS", src)
        self.assertIn("cuDNN 9", src)
        self.assertNotIn("cuDNN 8 на системе", src)
        self.assertIn('grid-template-columns:minmax(0,1fr) minmax(0,1fr)', src)
        self.assertIn('profile-box{min-width:0', src)

    def test_legacy_portable_script_has_no_deleted_launcher(self):
        src = self.read("scripts/make_portable_release.ps1")
        self.assertNotIn('"run_windows.ps1"', src)

    def test_build_script_isolated_from_legacy_output(self):
        src = self.read("scripts/build_exe.ps1")
        self.assertIn('"StreamVoiceBot"', src)
        self.assertIn("BUILD SUCCESS", src)
        self.assertIn("dist_release", src)
        self.assertIn(".build_venv", src)
        self.assertIn("torch==2.10.0+cpu", src)
        self.assertIn("https://download.pytorch.org/whl/cpu", src)
        self.assertIn("The existing dist/ folder will not be touched.", src)
        self.assertIn("Copy-Item $BuiltBundle $FinalStage -Recurse -Force", src)
        self.assertIn("Desktop shortcut: created automatically on first EXE launch.", src)
        self.assertIn("Get-ChildItem $DistRoot -Force", src)
        self.assertIn("FromBase64String", src)
        self.assertIn("UTF8.GetString", src)
        self.assertIn("UTF8Encoding]::new($true)", src)
        self.assertNotIn("Create_Desktop_Shortcut.cmd", src)
        self.assertNotIn("StreamVoiceBotCore.exe", src)

    def test_release_build_output_is_ignored(self):
        src = self.read(".gitignore")
        self.assertIn(".build_venv/", src)
        self.assertIn("dist_release/", src)
        self.assertIn("build/", src)


if __name__ == "__main__":
    unittest.main()
