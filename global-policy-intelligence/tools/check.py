"""Run reproducible checks from any working directory."""
from pathlib import Path
import subprocess
import sys


def main() -> int:
    tools = Path(__file__).resolve().parent
    commands = [
        ["tests/run_tests.py"],
        ["-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
        ["evals/make_pdf_fixture.py"],
        ["evals/run_retrieval_evals.py"],
    ]
    for command in commands:
        print("Running: " + " ".join(command), flush=True)
        result = subprocess.run([sys.executable, *command], cwd=tools, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
