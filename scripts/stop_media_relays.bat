@echo off
rem 用完即卸：杀掉 sing(9945)/photo(9946) 两个中继窗口进程，释放内存
rem （ComfyUI / IndexTTS 等重模型本身按需加载，中继只是轻量转发壳）
taskkill /fi "WINDOWTITLE eq fatfish-sing*" /f >nul 2>&1
taskkill /fi "WINDOWTITLE eq fatfish-photo*" /f >nul 2>&1
echo [fatfish-relays] sing/photo relays stopped.
exit /b 0
