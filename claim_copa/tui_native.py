"""Native dialogs and Windows clipboard in a separate, short-lived process."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def pick_files() -> dict:
    import tkinter as tk
    from tkinter import filedialog

    window = tk.Tk()
    window.withdraw()
    window.attributes("-topmost", True)
    try:
        paths = filedialog.askopenfilenames(
            parent=window, title="Claim-Agent — 첨부할 자료 선택",
            filetypes=[("발명 자료와 도면", "*.txt *.md *.png *.jpg *.jpeg *.webp *.yaml *.yml *.json *.csv"), ("모든 파일", "*.*")],
        )
        return {"paths": list(paths)}
    finally:
        window.destroy()


def clipboard(root: Path) -> dict:
    if os.name != "nt":
        return {"error": "이 버튼은 Windows용입니다. 터미널의 붙여넣기 또는 파일 선택을 사용하세요."}
    image_path = root / ".tui" / "clipboard" / (uuid.uuid4().hex + ".png")
    image_path.parent.mkdir(parents=True, exist_ok=True)
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
if ([System.Windows.Forms.Clipboard]::ContainsFileDropList()) {
    $paths = @([System.Windows.Forms.Clipboard]::GetFileDropList() | ForEach-Object { [string]$_ })
    @{ paths = $paths } | ConvertTo-Json -Compress
} elseif ([System.Windows.Forms.Clipboard]::ContainsImage()) {
    $image = [System.Windows.Forms.Clipboard]::GetImage()
    try { $image.Save($env:CLAIM_AGENT_CLIP_IMAGE, [System.Drawing.Imaging.ImageFormat]::Png) }
    finally { $image.Dispose() }
    @{ paths = @($env:CLAIM_AGENT_CLIP_IMAGE) } | ConvertTo-Json -Compress
} elseif ([System.Windows.Forms.Clipboard]::ContainsText()) {
    @{ text = [System.Windows.Forms.Clipboard]::GetText() } | ConvertTo-Json -Compress
} else { @{ error = '클립보드에 파일, 이미지 또는 텍스트가 없습니다.' } | ConvertTo-Json -Compress }
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-EncodedCommand", encoded],
        env={**os.environ, "CLAIM_AGENT_CLIP_IMAGE": str(image_path)},
        capture_output=True, encoding="utf-8", timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        return {"error": "클립보드를 읽지 못했습니다. 다시 복사한 뒤 시도하세요."}
    return json.loads(result.stdout.lstrip("\ufeff"))


def main() -> None:
    result = pick_files() if sys.argv[1] == "pick" else clipboard(Path(sys.argv[2]))
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
