import re
import subprocess
import sys

subprocess.run(
    ["adb", "shell", "uiautomator", "dump", "/sdcard/ui.xml"],
    capture_output=True,
)
xml = subprocess.run(
    ["adb", "shell", "cat", "/sdcard/ui.xml"],
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
).stdout

pattern = re.compile(r'text="([^"]*)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
needle = sys.argv[1] if len(sys.argv) > 1 else None
for match in pattern.finditer(xml):
    text = match.group(1)
    if not text:
        continue
    if needle and needle.lower() not in text.lower():
        continue
    x1, y1, x2, y2 = (int(match.group(i)) for i in range(2, 6))
    print(f"{text!r:45} center=({(x1 + x2) // 2},{(y1 + y2) // 2})  bounds=[{x1},{y1}][{x2},{y2}]")
