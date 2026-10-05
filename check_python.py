import subprocess, sys, os, time

# 获取所有python进程
try:
    output = subprocess.check_output(
        ["tasklist", "/FO", "CSV", "/FI", "IMAGENAME eq python.exe"], timeout=10
    ).decode("utf-8", errors="ignore")
    print("=== Python Processes ===")
    print(output)
except Exception as e:
    print(f"Error: {e}")

# 获取更详细信息（通过tasklist /V）
try:
    output = subprocess.check_output(
        ["tasklist", "/V", "/FO", "CSV", "/FI", "IMAGENAME eq python.exe"], timeout=10
    ).decode("utf-8", errors="ignore")
    lines = output.strip().split("\n")
    for line in lines:
        if "python" in line.lower():
            print(line)
except Exception as e:
    print(f"Error getting detailed info: {e}")
