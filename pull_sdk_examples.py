import os
import shutil
import subprocess
from pathlib import Path

# Settings
REPO_URL = "https://github.com/modelcontextprotocol/python-sdk.git"
REPO_DIR = Path(".temp_mcp_sdk")
SRC_SUBFOLDER = REPO_DIR / "examples" / "servers"
DEST_DIR = Path.cwd() / "servers"

def run_cmd(cmd, cwd=None):
    result = subprocess.run(cmd, shell=True, check=True, cwd=cwd)
    return result

def clone_or_update_repo():
    if REPO_DIR.exists():
        print(f"Updating existing repo in {REPO_DIR}...")
        run_cmd("git pull", cwd=REPO_DIR)
    else:
        print(f"Cloning repo into {REPO_DIR}...")
        run_cmd(f"git clone --depth 1 {REPO_URL} {REPO_DIR}")

def copy_examples():
    if not SRC_SUBFOLDER.exists():
        raise FileNotFoundError(f"Source folder not found: {SRC_SUBFOLDER}")
    
    print(f"Copying from {SRC_SUBFOLDER} to {DEST_DIR}...")
    if DEST_DIR.exists():
        shutil.rmtree(DEST_DIR)
    shutil.copytree(SRC_SUBFOLDER, DEST_DIR)

def main():
    print(">>> Pulling MCP Python SDK examples...")
    clone_or_update_repo()
    copy_examples()
    print("✅ Done.")

if __name__ == "__main__":
    main()
