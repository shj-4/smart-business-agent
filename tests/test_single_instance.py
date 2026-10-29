import subprocess
import sys
from pathlib import Path

import pytest

from bot.single_instance import BotAlreadyRunning, acquire_polling_lock, release_polling_lock


def test_polling_lock_blocks_second_process(tmp_path):
    lock_path = tmp_path / "bot.lock"
    script = (
        "import sys,time; from bot.single_instance import acquire_polling_lock; "
        "acquire_polling_lock(sys.argv[1]); print('locked', flush=True); time.sleep(15)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path)],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(BotAlreadyRunning):
            acquire_polling_lock(lock_path)
    finally:
        process.terminate()
        process.wait(timeout=10)
        release_polling_lock()

