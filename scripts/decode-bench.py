#!/usr/bin/env python3
"""Time two rars CLI builds, and other RAR readers, on the same archives.

    decode-bench.py data DIR                    write the test folder
    decode-bench.py pack DIR OUT RARS           make the three solid archives
    decode-bench.py time OUT NAME=CMD ...       time test and extract, in turn

The folder is like the one these numbers were first taken on: real text (the
Cargo registry sources), incompressible blocks and mostly-zero files, about
200 MB in some 9000 files. Each reader runs once per round, in turn, three
rounds, so a busy runner slows them alike.
"""
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

TEXT_BYTES = 100 << 20
RANDOM_BYTES = 60 << 20
ZERO_BYTES = 40 << 20


def data(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    registry = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo")) / "registry" / "src"
    total = 0
    n = 0
    for path in sorted(registry.rglob("*")):
        if total >= TEXT_BYTES:
            break
        if path.suffix not in {".rs", ".md", ".toml", ".txt"} or not path.is_file():
            continue
        size = path.stat().st_size
        if not 0 < size < 1 << 20:
            continue
        dest = out / "text" / f"{n // 1000:02}" / f"{n:05}{path.suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        total += size
        n += 1
    rng = random.Random(1)
    for kind, budget in (("random", RANDOM_BYTES), ("zeros", ZERO_BYTES)):
        made = 0
        i = 0
        while made < budget:
            size = rng.choice((4 << 10, 64 << 10, 512 << 10, 2 << 20))
            if kind == "random":
                body = rng.randbytes(size)
            else:
                body = bytearray(size)
                for _ in range(size // 4096):
                    body[rng.randrange(size)] = rng.randrange(256)
            dest = out / kind / f"{i:05}.bin"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
            made += size
            i += 1
        n += i
    print(f"{n} files, {total >> 20} MiB of text")


def pack(src: Path, out: Path, rars: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, args in (
        ("rar5", ["--format", "rar50"]),
        ("rar29-lz", ["--format", "rar29", "--level", "2"]),
        ("rar29-ppmd", ["--format", "rar29"]),
    ):
        archive = out / f"{name}.rar"
        archive.unlink(missing_ok=True)
        subprocess.run([rars, "--progress", "never", "add", "--solid", *args, str(archive), str(src)], check=True)
        print(f"{name}: {archive.stat().st_size >> 20} MiB")


def run(cmd: list[str]) -> float | None:
    """Seconds taken, or None if the reader reported an error."""
    start = time.perf_counter()
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
    if result.returncode:
        first = next((l for l in result.stderr.splitlines() if l.strip()), "")
        print(f"error ({result.returncode}) from {' '.join(cmd)}: {first}", flush=True)
        return None
    return time.perf_counter() - start


def commands(spec: str, archive: Path, dest: Path) -> tuple[list[str], list[str]]:
    name, cmd = spec.split("=", 1)
    kind, exe = cmd.split(":", 1)
    if kind == "rars":
        base = [exe, "--progress", "never", "--threads", "1"]
        return base + ["test", str(archive)], base + ["extract", str(archive), str(dest)]
    if kind == "unrar":
        return [exe, "t", "-inul", str(archive)], [exe, "x", "-inul", "-o+", str(archive), str(dest) + os.sep]
    if kind == "7z":
        return [exe, "t", str(archive)], [exe, "x", "-y", f"-o{dest}", str(archive)]
    raise SystemExit(f"unknown reader kind {kind}")


def timing(out: Path, specs: list[str]) -> None:
    unpacked = int(os.environ["BENCH_UNPACKED"]) if os.environ.get("BENCH_UNPACKED") else sum(
        p.stat().st_size for p in (out.parent / "data").rglob("*") if p.is_file())
    results: dict[tuple[str, str, str], list[float]] = {}
    for archive in sorted(out.glob("*.rar")):
        for _ in range(3):
            for spec in specs:
                name = spec.split("=", 1)[0]
                dest = out.parent / "extracted"
                shutil.rmtree(dest, ignore_errors=True)
                dest.mkdir()
                test_cmd, extract_cmd = commands(spec, archive, dest)
                for what, cmd in (("test", test_cmd), ("extract", extract_cmd)):
                    seconds = run(cmd)
                    rate = None if seconds is None else unpacked / seconds / 1e6
                    results.setdefault((archive.stem, what, name), []).append(rate)
                shutil.rmtree(dest, ignore_errors=True)
    lines = ["| archive | what | " + " | ".join(s.split("=", 1)[0] for s in specs) + " |",
             "|---|---|" + "---|" * len(specs)]
    for archive in sorted({k[0] for k in results}):
        for what in ("test", "extract"):
            cells = []
            for spec in specs:
                runs = [r for r in results[(archive, what, spec.split("=", 1)[0])] if r is not None]
                cells.append(f"{min(runs):.0f}–{max(runs):.0f}" if runs else "error")
            lines.append(f"| {archive} | {what} | " + " | ".join(cells) + " |")
    table = "\n".join(lines)
    print(f"MB/s of unpacked data ({unpacked / 1e6:.0f} MB), three rounds in turn:\n\n{table}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### {os.environ.get('RUNNER_OS', '')} {os.environ.get('RUNNER_ARCH', '')}\n\n"
                    f"MB/s of unpacked data ({unpacked / 1e6:.0f} MB), three rounds in turn:\n\n{table}\n")


def main() -> None:
    mode = sys.argv[1]
    if mode == "data":
        data(Path(sys.argv[2]))
    elif mode == "pack":
        pack(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
    elif mode == "time":
        timing(Path(sys.argv[2]), sys.argv[3:])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
