"""Package an explicitly supplied custom binary; never install or publish it."""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    with args.exe.open("rb") as stream:
        exe_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    provenance = {
        "upstream_commit": "29eb79e9866b3262a777fdf8195c1b9e63684c91",
        "upstream_tag": "rust-v0.170.0",
        "build_run": args.run,
        "binary_sha256": exe_hash,
        "custom_build": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "x", zipfile.ZIP_DEFLATED) as archive:
        archive.write(args.exe, "codex.exe")
        for name in ("LICENSE", "NOTICE", "MODIFICATIONS.txt"):
            archive.write(root / "redistribution" / name, name)
        for name in ("compact-peer-messages.patch", "workspace-release-lock.patch"):
            archive.write(root / name, "source/" + name)
        archive.writestr("BUILD.json", json.dumps(provenance, indent=2) + "\n")
    with zipfile.ZipFile(args.output) as archive:
        assert archive.testzip() is None
        assert hashlib.sha256(archive.read("codex.exe")).hexdigest() == exe_hash
    with args.output.open("rb") as stream:
        zip_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(
        f"{zip_hash}  {args.output.name}\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps({"zip": str(args.output), "sha256": zip_hash, **provenance}))


if __name__ == "__main__":
    main()
