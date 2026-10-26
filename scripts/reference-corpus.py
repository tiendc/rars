#!/usr/bin/env python3
r"""Pack a corpus with real RAR packers, then hold rars to unrar on it.

`generate` runs each packer over the switch combinations real archives use and
writes the archives with a manifest. `compare` extracts every archive, and the
realistic damage variants of each volume set, with unrar and with rars, and
reports each case where the two disagree: one accepts what the other refuses,
or both accept with different bytes.

Only what a real packer writes goes in the corpus, and only with switches
people use (see `combinations`). Packers are given as NAME=COMMAND, where
COMMAND may carry a prefix such as `wine /opt/winrar/Rar.exe`; the packer's own
banner gives its version, which decides the formats it is asked for.

    python3 scripts/reference-corpus.py generate target/rar-corpus \
        --packer rar-7.23=/opt/rar-7.23/rar \
        --packer winrar-6.24="wine /opt/winrar-6.24/Rar.exe"
    python3 scripts/reference-corpus.py compare target/rar-corpus \
        --unrar /opt/rar-7.23/unrar --rars target/release/rars

A failure counts as agreement when both tools refuse; their messages and any
partial output are not compared. target/ is ignored, so the corpus can live
there.
"""
import argparse
import concurrent.futures
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile

PASSWORD = "corpus-password"
WRONG_PASSWORD = "not-the-password"
VOLUME_SIZE = "400k"
# A lost usenet article as a downloader writes it: zero bytes in its place.
ARTICLE_GAP = 64 * 1024


def noise(length, seed):
    # Fixed integer arithmetic: identical bytes across Python versions/hosts.
    state = seed
    output = bytearray()
    for _ in range(length):
        state ^= (state << 13) & 0xFFFFFFFFFFFFFFFF
        state ^= state >> 7
        state ^= (state << 17) & 0xFFFFFFFFFFFFFFFF
        output.append(state & 255)
    return bytes(output)


def x86_code(length, seed):
    """Bytes shaped like x86 code: short instructions and frequent relative
    calls and jumps into the same buffer, which packers give an E8/E8E9
    filter."""
    filler = noise(length, seed)
    output = bytearray()
    index = 0
    while len(output) < length:
        step = filler[index % len(filler)]
        index += 1
        if step < 40:
            target = (filler[index % len(filler)] << 8 | step) * 37 % length
            relative = (target - len(output) - 5) & 0xFFFFFFFF
            output += bytes([0xE8 if step & 1 else 0xE9]) + struct.pack("<I", relative)
        else:
            output += bytes([0x48, 0x89, 0xC0 | step & 0x3F][: 1 + step % 3])
    return bytes(output[:length])


def wav_audio(seconds, rate=22050):
    """16-bit stereo PCM, which packers give a delta filter."""
    frames = bytearray()
    for n in range(seconds * rate):
        left = int(12000 * math.sin(2 * math.pi * 220 * n / rate))
        right = int(9000 * math.sin(2 * math.pi * 330 * n / rate))
        frames += struct.pack("<hh", left, right)
    header = b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 2, rate, rate * 4, 4, 16)
    return header + b"data" + struct.pack("<I", len(frames)) + bytes(frames)


def bmp_image(width, height):
    """A 24-bit true-colour bitmap, which RAR 4 packers can give an RGB filter."""
    row_size = (width * 3 + 3) & ~3
    pixels = bytearray()
    for y in range(height):
        row = bytearray()
        for x in range(width):
            row += bytes(((x * 255) // width, (y * 255) // height, ((x + y) * 7) & 255))
        pixels += row + bytes(row_size - len(row))
    header = b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54)
    header += struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
    return header + bytes(pixels)


class Link(str):
    """A payload entry that is a symbolic link to this relative target."""


def nfo(title):
    lines = [title, "=" * len(title), ""]
    lines += ["%-12s: %s" % (key, value) for key, value in (
        ("Source", "corpus"), ("Format", "reference"), ("Packer", "native RAR"))]
    return ("\r\n".join(lines * 8) + "\r\n").encode("ascii")


# Two kinds of content real archives carry. A release is one large media file
# with an .nfo and a sample, stored or compressed but never solid. Software is
# many smaller files, code and audio among them, which packers filter and pack
# solid.
PAYLOADS = {
    "release": lambda: {
        "Feature.2026.mkv": noise(1_100_011, 0x51E4A5E),
        "Feature.2026.nfo": nfo("Feature.2026"),
        "Sample/feature.2026.sample.mkv": noise(90_007, 0x5A3B1E),
    },
    "software": lambda: {
        "Setup/setup.exe": x86_code(700_001, 0xE8E9),
        "Setup/data/theme.wav": wav_audio(3),
        "Setup/data/assets.pak": noise(900_019, 0xA55E75),
        "Setup/readme.txt": b"Install with setup.exe.\r\n" * 400,
        "Setup/license.txt": b"Licensed for testing only.\r\n" * 150,
        "Setup/empty.dat": b"",
    },
}


# Content for the extended cases (generate --extended): what the switch
# combinations above leave out but real archives carry.
EXTENDED_PAYLOADS = {
    # Names outside ASCII, as packers store them (RAR 4 in its Unicode field).
    "names": lambda: {
        "Tài liệu/báo cáo năm 2026.txt": "Nội dung tiếng Việt.\n".encode() * 200,
        "日本語/ファイル名.txt": "日本語のテキスト\n".encode() * 200,
        "Ελληνικά/αρχείο.txt": "Ελληνικό κείμενο\n".encode() * 200,
        "🎉 party/emoji name 😀.txt": b"emoji\n" * 200,
        "spaces and (brackets) [1]/a b c.txt": b"spaces\n" * 200,
        ("long-" + "x" * 180 + ".txt"): b"a long name\n" * 50,
    },
    # True colour and audio, for the RGB and audio filters (-mcc+ -mca+).
    "media": lambda: {
        "image.bmp": bmp_image(384, 256),
        "theme.wav": wav_audio(3),
    },
    # The same bytes three times, for references to identical files (-oi).
    "dupes": lambda: dict.fromkeys(
        ("one/copy.bin", "two/copy.bin", "three/copy.bin"), noise(200_003, 0xD0B1E)
    ) | {"unique.bin": noise(50_021, 0x0D0)},
    # A symbolic link to a file next to it, stored as a link (-ol).
    "links": lambda: {
        "real.txt": b"the link points here\n" * 100,
        "link-to-real.txt": Link("real.txt"),
        "dir/inner.txt": b"inside a folder\n" * 100,
    },
}


def extended_combinations(version):
    """What `combinations` does not ask for: BLAKE2 hashes, quick open
    records, references to identical files, links, names outside ASCII,
    PPMd and the RAR 4 multimedia filters, locked archives with comments,
    large dictionaries, and recovery volumes."""
    major = version[0]
    formats = []
    if major < 7:
        formats.append(("rar4", ["-ma4"] if major >= 5 else []))
    if major >= 5:
        formats.append(("rar5", ["-ma5"]))
    for format_name, format_switches in formats:
        cases = [
            ("names", "m3", ["-m3"], False, False),
            ("names", "m3-hp", ["-m3", "-hp" + PASSWORD], True, False),
            ("links", "m3-ol", ["-m3", "-ol"], False, False),
            ("software", "m3-k-z", ["-m3", "-k", "-z{comment}"], False, False),
            ("release", "m3-vol-rv", ["-m3", "-v" + VOLUME_SIZE, "-rv2"], False, True),
        ]
        if format_name == "rar4":
            cases += [
                ("software", "m5-mct", ["-m5", "-mct+"], False, False),
                ("software", "m5s-mct", ["-m5", "-s", "-mct+"], False, False),
                ("media", "m5-mcc-mca", ["-m5", "-mcc+", "-mca+"], False, False),
            ]
        else:
            cases += [
                ("software", "m3s-htb", ["-m3", "-s", "-htb"], False, False),
                ("software", "m3-qo", ["-m3", "-qo+"], False, False),
                ("dupes", "m3-oi", ["-m3", "-oi1"], False, False),
                ("software", "m5s-md1g", ["-m5", "-s", "-md1g"], False, False),
            ]
        for payload, label, switches, encrypted, volumes in cases:
            yield {
                "name": "-".join((format_name, payload, label)),
                "payload": payload,
                "encrypted": encrypted,
                "volumes": volumes,
                "switches": format_switches + switches,
            }


def combinations(version):
    """The switch sets a packer of this version is asked for.

    Formats are those the version writes: RAR 4 up to 6.x (with -ma4 from 5.0),
    RAR 5 from 5.0. Not asked for, as no real archive is made that way:
    solid storing (-s -m0), solid releases, and old-style volume names for
    RAR 5, which packers ignore."""
    major = version[0]
    formats = []
    if major < 7:
        formats.append(("rar4", ["-ma4"] if major >= 5 else []))
    if major >= 5:
        formats.append(("rar5", ["-ma5"]))
    for format_name, format_switches in formats:
        for payload in PAYLOADS:
            methods = [("m0", ["-m0"]), ("m3", ["-m3"]), ("m5", ["-m5"])]
            if payload == "software":
                methods += [("m3s", ["-m3", "-s"]), ("m5s", ["-m5", "-s"])]
            for method_name, method_switches in methods:
                for crypt_name, crypt_switches in (
                        ("plain", []), ("p", ["-p" + PASSWORD]), ("hp", ["-hp" + PASSWORD])):
                    volume_layouts = [("single", []), ("vol", ["-v" + VOLUME_SIZE])]
                    if format_name == "rar4":
                        # .rar, .r00, .r01...; RAR 5 packers ignore -vn.
                        volume_layouts.append(("volold", ["-v" + VOLUME_SIZE, "-vn"]))
                    for volume_name, volume_switches in volume_layouts:
                        for recovery_name, recovery_switches in (("", []), ("rr", ["-rr5%"])):
                            name = "-".join(filter(None, (
                                format_name, payload, method_name, crypt_name,
                                volume_name, recovery_name)))
                            yield {
                                "name": name,
                                "payload": payload,
                                "encrypted": crypt_name != "plain",
                                "volumes": volume_name != "single",
                                "switches": format_switches + method_switches + crypt_switches
                                + volume_switches + recovery_switches,
                            }


def packer_version(command):
    banner = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL).stdout.decode("latin-1")
    match = re.search(r"RAR (\d+)\.(\d+)", banner)
    if not match:
        raise SystemExit("not a RAR packer: " + shlex.join(command))
    return int(match.group(1)), int(match.group(2))


def write_payload(directory, files):
    for relative, data in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, Link):
            os.symlink(str(data), path)
        else:
            path.write_bytes(data)


def payload_hashes(files):
    """What extraction should give: a link reads as the file it points to."""
    hashes = {}
    for relative, data in files.items():
        if isinstance(data, Link):
            data = files[(Path(relative).parent / data).as_posix()]
        hashes[relative] = hashlib.sha256(data).hexdigest()
    return hashes


def archive_volumes(directory, stem):
    names = sorted(path.name for path in directory.iterdir() if path.name.startswith(stem + "."))
    first = [name for name in names if name in (stem + ".rar", stem + ".part1.rar",
                                                 stem + ".part01.rar", stem + ".part001.rar")]
    if len(first) != 1:
        raise RuntimeError("cannot find the first volume of %s in %s" % (stem, names))
    return first[0], volume_order(first[0], names)


def volume_order(first, names):
    """Volumes in set order. By name, old-style .r00 and .r01 sort before
    their .rar, which comes first in the set."""
    return [first] + sorted(name for name in names if name != first)


def pack(packer, command, case, payload_dir, out_dir):
    stem = "%s-%s" % (packer, case["name"])
    target = out_dir / stem
    target.mkdir()
    source = payload_dir / case["payload"]
    # Absolute: the packer runs inside the payload's folder.
    switches = [switch.replace("{comment}", str((payload_dir.parent / "comment.txt").resolve()))
                for switch in case["switches"]]
    result = subprocess.run(
        command + ["a", "-r", "-idq", "-y"] + switches
        # Relative, as Windows packers read a leading "/" as a switch.
        + [os.path.relpath(target / (stem + ".rar"), source), "*"],
        cwd=source, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise RuntimeError("%s failed (%d): %s" % (stem, result.returncode,
                                                   result.stdout.decode("latin-1")[-400:]))
    first, volumes = archive_volumes(target, stem)
    return dict(case, packer=packer, directory=stem, first=first, volumes=volumes)


def generate(args):
    jobs = []
    packers = {}
    for spec in args.packer:
        name, _, command = spec.partition("=")
        command = shlex.split(command)
        if not name or not command or name in packers:
            raise SystemExit("--packer needs a new NAME=COMMAND: " + spec)
        version = packer_version(command)
        packers[name] = {"command": command, "version": "%d.%02d" % version}
        jobs += [(name, command, case) for case in combinations(version)]
        if args.extended:
            jobs += [(name, command, case) for case in extended_combinations(version)]
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=False)
    payload_dir = out_dir / "payload"
    payloads = {}
    builders = dict(PAYLOADS, **(EXTENDED_PAYLOADS if args.extended else {}))
    for name, build in builders.items():
        files = build()
        write_payload(payload_dir / name, files)
        payloads[name] = payload_hashes(files)
    (out_dir / "comment.txt").write_text("An archive comment, as -z stores it.\r\n")
    archives_dir = out_dir / "archives"
    archives_dir.mkdir()
    archives = []
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
        futures = [pool.submit(pack, name, command, case, payload_dir, archives_dir)
                   for name, command, case in jobs]
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            archives.append(future.result())
            if done % 100 == 0:
                print("packed %d of %d" % (done, len(futures)), file=sys.stderr)
    archives.sort(key=lambda archive: archive["directory"])
    manifest = {"password": PASSWORD, "packers": packers, "payloads": payloads,
                "archives": archives}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print("%d archives from %d packer%s in %s" % (
        len(archives), len(packers), "" if len(packers) == 1 else "s", out_dir))


def damage_variants(archive):
    """The realistic ways a download goes wrong, as (name, password, change).
    Each change takes the copied volume paths and alters them in place."""
    yield "intact", PASSWORD if archive["encrypted"] else None, None
    if archive["encrypted"]:
        yield "wrong-password", WRONG_PASSWORD, None
    if archive["volumes"] and len(archive["volumes"]) >= 3:
        def missing(paths):
            paths[len(paths) // 2].unlink()

        def truncated(paths):
            last = paths[-1]
            last.write_bytes(last.read_bytes()[: last.stat().st_size // 2])

        def article_gap(paths):
            middle = paths[len(paths) // 2]
            data = bytearray(middle.read_bytes())
            start = len(data) // 2
            data[start:start + ARTICLE_GAP] = bytes(len(data[start:start + ARTICLE_GAP]))
            middle.write_bytes(bytes(data))

        password = PASSWORD if archive["encrypted"] else None
        yield "missing-volume", password, missing
        yield "truncated-last-volume", password, truncated
        yield "zeroed-article", password, article_gap


def tree(directory):
    files = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            files[path.relative_to(directory).as_posix()] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return files


def run(command, timeout):
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timed out"
    return result.returncode, result.stdout.decode("utf-8", "replace").strip()[-600:]


def compare_one(corpus, archive, variant, expected, unrar, rars, rars_options, timeout):
    name, password, change = variant
    with tempfile.TemporaryDirectory(prefix="rars-corpus-") as work:
        work = Path(work)
        volumes = work / "volumes"
        volumes.mkdir()
        source = corpus / "archives" / archive["directory"]
        paths = []
        for volume in volume_order(archive["first"], archive["volumes"]):
            shutil.copyfile(source / volume, volumes / volume)
            paths.append(volumes / volume)
        if change is not None:
            change(paths)
        first = str(volumes / archive["first"])
        unrar_out, rars_out = work / "unrar", work / "rars"
        unrar_out.mkdir()
        rars_out.mkdir()
        unrar_code, unrar_log = run(
            unrar + ["x", "-idq", "-y", "-o+", "-p" + password if password else "-p-",
                     first, str(unrar_out) + os.sep], timeout)
        rars_code, rars_log = run(
            rars + ["extract"] + rars_options + (["-p", password] if password else [])
            + [first, str(rars_out)],
            timeout)
        unrar_ok, rars_ok = unrar_code == 0, rars_code == 0
        if rars_code is None or unrar_code is None:
            # A hang is never agreement, whatever the other tool did.
            outcome = "rars-timeout" if rars_code is None else "unrar-timeout"
        elif unrar_ok and rars_ok:
            unrar_files, rars_files = tree(unrar_out), tree(rars_out)
            if unrar_files != rars_files:
                outcome = "different-bytes"
            elif name == "intact" and unrar_files != expected:
                # Both tools agree, but not with what was packed.
                outcome = "both-wrong-bytes"
            else:
                outcome = "agree"
        elif unrar_ok:
            outcome = "rars-refuses"
        elif rars_ok:
            outcome = "rars-accepts"
        else:
            outcome = "agree"
        return {"archive": archive["directory"], "packer": archive["packer"], "case": name,
                "outcome": outcome, "unrar": [unrar_code, unrar_log],
                "rars": [rars_code, rars_log]}


def compare(args):
    corpus = Path(args.corpus)
    manifest = json.loads((corpus / "manifest.json").read_text())
    unrar, rars = shlex.split(args.unrar), shlex.split(args.rars)
    archives = [archive for archive in manifest["archives"]
                if not args.match or re.search(args.match, archive["directory"])]
    results = []
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
        futures = [pool.submit(compare_one, corpus, archive, variant,
                               manifest["payloads"][archive["payload"]], unrar, rars,
                               shlex.split(args.rars_options), args.timeout)
                   for archive in archives for variant in damage_variants(archive)]
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            results.append(future.result())
            if done % 200 == 0:
                print("compared %d of %d" % (done, len(futures)), file=sys.stderr)
    results.sort(key=lambda result: (result["archive"], result["case"]))
    if args.results:
        Path(args.results).write_text(json.dumps(results, indent=1) + "\n")
    deviations = [result for result in results if result["outcome"] != "agree"]
    counts = {}
    for result in deviations:
        key = (result["outcome"], result["packer"], result["case"])
        counts[key] = counts.get(key, 0) + 1
    for (outcome, packer, case), count in sorted(counts.items()):
        print("%-16s %-14s %-22s %d" % (outcome, packer, case, count))
    for result in deviations[: args.show]:
        print("\n%s [%s] %s\n  unrar %s: %s\n  rars  %s: %s" % (
            result["archive"], result["case"], result["outcome"], result["unrar"][0],
            result["unrar"][1][-200:], result["rars"][0], result["rars"][1][-300:]))
    print("\n%d cases, %d agree, %d deviate" % (
        len(results), len(results) - len(deviations), len(deviations)))
    return 1 if deviations else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate", help="pack the corpus with real packers")
    gen.add_argument("output", help="new directory for the corpus")
    gen.add_argument("--packer", action="append", required=True, metavar="NAME=COMMAND",
                     help="a native RAR packer, e.g. rar-7.23=/opt/rar/rar (repeatable)")
    gen.add_argument("--jobs", type=int, default=os.cpu_count())
    gen.add_argument("--extended", action="store_true",
                     help="also pack the extended cases (see extended_combinations)")
    cmp = commands.add_parser("compare", help="extract the corpus with unrar and rars")
    cmp.add_argument("corpus")
    cmp.add_argument("--unrar", required=True, help="unrar command, the reference")
    cmp.add_argument("--rars", required=True, help="rars command")
    cmp.add_argument("--rars-options", default="",
                     help="extract options for rars, e.g. '--rar50-buffered-decode-limit 64k' "
                     "to stream filtered members as it does those above the default limit")
    cmp.add_argument("--match", help="only archives whose name matches this regex")
    cmp.add_argument("--results", help="write every case's result to this JSON file")
    cmp.add_argument("--show", type=int, default=20, help="deviations to print in full")
    cmp.add_argument("--timeout", type=int, default=300)
    cmp.add_argument("--jobs", type=int, default=os.cpu_count())
    args = parser.parse_args()
    if args.command == "generate":
        generate(args)
        return 0
    return compare(args)


if __name__ == "__main__":
    sys.exit(main())
