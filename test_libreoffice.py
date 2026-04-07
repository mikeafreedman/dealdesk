import subprocess
from pathlib import Path

soffice = r"C:\Program Files\LibreOffice\program\soffice.exe"
print("Path exists:", Path(soffice).exists())

try:
    result = subprocess.run(
        [soffice, "--version"],
        capture_output=True,
        text=True,
        timeout=10
    )
    print("Return code:", result.returncode)
    print("stdout:", result.stdout)
    print("stderr:", result.stderr)
except FileNotFoundError as e:
    print("FileNotFoundError:", e)
except Exception as e:
    print("Other error:", e)
