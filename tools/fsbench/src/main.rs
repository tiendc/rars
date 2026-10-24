//! fsbench <dir> [files] [bytes]: write `files` files of `bytes` bytes under
//! `dir` in nested folders, once per pattern, and report the cost per file.
//!
//! The patterns add, one at a time, the steps the rars CLI takes per file
//! (from strace on Linux) to the few that 7-Zip and UnRAR take.

use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime};

#[derive(Clone, Copy)]
struct Steps {
    /// create_dir_all on the parent for every file, not once per folder.
    mkdir_each: bool,
    /// lstat every component of the path, this many times.
    lstat_rounds: u32,
    /// canonicalize the destination's ancestors, this many times.
    canonicalize_rounds: u32,
    /// Set the time by opening the file again after it is closed.
    reopen_for_time: bool,
    /// Write into a staging folder, then hard-link into place and unlink.
    staged: bool,
    /// Open and close the archive again for every file.
    reopen_archive: bool,
}

const NONE: Steps = Steps {
    mkdir_each: false,
    lstat_rounds: 0,
    canonicalize_rounds: 0,
    reopen_for_time: false,
    staged: false,
    reopen_archive: false,
};

fn patterns() -> Vec<(&'static str, Steps)> {
    vec![
        ("minimal (7-Zip/UnRAR-like)", NONE),
        ("lstat each component x1 (spike safe-path)", Steps { lstat_rounds: 1, ..NONE }),
        ("+ mkdir per file", Steps { mkdir_each: true, ..NONE }),
        ("+ lstat each component x4", Steps { lstat_rounds: 4, ..NONE }),
        ("+ canonicalize x5", Steps { canonicalize_rounds: 5, ..NONE }),
        ("+ reopen to set time", Steps { reopen_for_time: true, ..NONE }),
        ("+ staging, link, unlink", Steps { staged: true, ..NONE }),
        ("+ reopen archive per file", Steps { reopen_archive: true, ..NONE }),
        (
            "all (rars CLI-like)",
            Steps {
                mkdir_each: true,
                lstat_rounds: 4,
                canonicalize_rounds: 5,
                reopen_for_time: true,
                staged: true,
                reopen_archive: true,
            },
        ),
    ]
}

fn relative(i: usize) -> PathBuf {
    PathBuf::from(format!("data/text/{:02}/{:05}.rs", i / 100, i))
}

fn run(root: &Path, archive: &Path, files: usize, data: &[u8], steps: Steps) -> std::io::Result<Duration> {
    let _ = fs::remove_dir_all(root);
    fs::create_dir_all(root)?;
    let staging = root.join(".staging");
    if steps.staged {
        fs::create_dir(&staging)?;
    }
    let time = SystemTime::UNIX_EPOCH + Duration::from_secs(1_700_000_000);
    let mut made = std::collections::HashSet::new();
    let start = Instant::now();
    for i in 0..files {
        let rel = relative(i);
        let dest = root.join(&rel);
        let parent = dest.parent().expect("parent");
        for _ in 0..steps.lstat_rounds {
            let mut p = root.to_path_buf();
            for c in rel.components() {
                p.push(c);
                let _ = fs::symlink_metadata(&p);
            }
        }
        for _ in 0..steps.canonicalize_rounds {
            let mut a = Some(dest.as_path());
            while let Some(p) = a {
                if fs::canonicalize(p).is_ok() {
                    break;
                }
                a = p.parent();
            }
        }
        if steps.reopen_archive {
            drop(File::open(archive)?);
        }
        let target = if steps.staged { staging.join(&rel) } else { dest.clone() };
        let target_parent = target.parent().expect("parent").to_path_buf();
        if steps.mkdir_each || made.insert(target_parent.clone()) {
            fs::create_dir_all(&target_parent)?;
        }
        let mut file = OpenOptions::new().write(true).create_new(true).open(&target)?;
        file.write_all(data)?;
        if !steps.reopen_for_time {
            file.set_modified(time)?;
        }
        drop(file);
        if steps.staged {
            if steps.mkdir_each || made.insert(parent.to_path_buf()) {
                fs::create_dir_all(parent)?;
            }
            fs::hard_link(&target, &dest)?;
            fs::remove_file(&target)?;
        }
        if steps.reopen_for_time {
            OpenOptions::new().write(true).open(&dest)?.set_modified(time)?;
        }
    }
    let elapsed = start.elapsed();
    fs::remove_dir_all(root)?;
    Ok(elapsed)
}

/// Files created relative to an open handle on their folder (openat-style,
/// through cap-std), which neither resolves the path again nor can be sent
/// through a link: the folder handles are opened once and kept.
fn run_cap_std(root: &Path, files: usize, data: &[u8]) -> std::io::Result<Duration> {
    use cap_std::ambient_authority;
    use cap_std::fs::{Dir, OpenOptions as CapOpen};
    let _ = fs::remove_dir_all(root);
    fs::create_dir_all(root)?;
    let time = SystemTime::UNIX_EPOCH + Duration::from_secs(1_700_000_000);
    let start = Instant::now();
    let base = Dir::open_ambient_dir(root, ambient_authority())?;
    let mut folders: std::collections::HashMap<PathBuf, Dir> = std::collections::HashMap::new();
    for i in 0..files {
        let rel = relative(i);
        let parent = rel.parent().expect("parent").to_path_buf();
        if !folders.contains_key(&parent) {
            base.create_dir_all(&parent)?;
            folders.insert(parent.clone(), base.open_dir(&parent)?);
        }
        let dir = &folders[&parent];
        let name = rel.file_name().expect("name");
        let mut file = dir
            .open_with(name, CapOpen::new().write(true).create_new(true))?
            .into_std();
        file.write_all(data)?;
        file.set_modified(time)?;
    }
    drop(folders);
    let elapsed = start.elapsed();
    fs::remove_dir_all(root)?;
    Ok(elapsed)
}

fn main() -> std::io::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let dir = PathBuf::from(args.first().map_or("fsbench-work", String::as_str));
    let files: usize = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(2400);
    let bytes: usize = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(60_000);
    fs::create_dir_all(&dir)?;
    let archive = dir.join("archive.bin");
    fs::write(&archive, vec![7u8; 64 << 20])?;
    let data: Vec<u8> = (0..bytes).map(|i| (i * 31 % 251) as u8).collect();
    println!("{files} files of {bytes} bytes; ms per 1000 files (min of 3), MB/s");
    println!("| pattern | ms / 1000 files | MB/s |");
    println!("|---|---|---|");
    for (name, steps) in patterns() {
        let mut best = Duration::MAX;
        for _ in 0..3 {
            best = best.min(run(&dir.join("out"), &archive, files, &data, steps)?);
        }
        let per_k = best.as_secs_f64() * 1000.0 * 1000.0 / files as f64;
        let mbps = (files * bytes) as f64 / best.as_secs_f64() / 1e6;
        println!("| {name} | {per_k:.0} | {mbps:.0} |");
    }
    let mut best = Duration::MAX;
    for _ in 0..3 {
        best = best.min(run_cap_std(&dir.join("out"), files, &data)?);
    }
    let per_k = best.as_secs_f64() * 1000.0 * 1000.0 / files as f64;
    let mbps = (files * bytes) as f64 / best.as_secs_f64() / 1e6;
    println!("| cap-std, folder handles kept | {per_k:.0} | {mbps:.0} |");
    fs::remove_file(&archive)?;
    Ok(())
}
