@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Run start_bot.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "from pathlib import Path; import torch; p=Path('models')/'v5_ru.pt'; print('Torch:',torch.__version__); print('CUDA:',torch.cuda.is_available()); print('Model:', p.resolve(), p.exists(), p.stat().st_size if p.exists() else 0)"
pause
