"""Measure conservative characters-per-token ratios for bridge planning.

This is development tooling, not a runtime dependency. Install ``tiktoken``
in the environment used to run the measurement. The corpus is generated so
the result is reproducible without access to private provider transcripts.
"""

from __future__ import annotations

import json
import statistics
import textwrap
import uuid
from base64 import b64encode
from hashlib import sha1

try:
    import tiktoken
except ImportError as error:  # pragma: no cover - developer guidance
    raise SystemExit("install tiktoken to run this development measurement") from error


def corpus() -> dict[str, str]:
    return {
        "prose": (
            "The session bridge preserves identity while moving an engineering "
            "conversation between native harnesses. "
            * 120
        ),
        "python": textwrap.dedent(
            """\
            @dataclass(frozen=True, slots=True)
            class Budget:
                tokens: int
                chars: int
                origin: str

            def resolve_budget(policy: BudgetPolicy, configured: int | None) -> Budget:
                return Budget(
                    tokens=max(configured or 0, 1024),
                    chars=policy.cost(configured),
                    origin="target-default",
                )
            """
        )
        * 80,
        "json": "\n".join(
            json.dumps(
                {
                    "id": str(uuid.UUID(int=index)),
                    "type": "response_item",
                    "payload": {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": f"item {index}"}],
                    },
                }
            )
            for index in range(180)
        ),
        "diff": (
            "@@ -10,7 +10,9 @@\n"
            "-old_value = resolve(source)\n"
            "+budget = resolve_budget(target, config)\n"
            "+selected = select_messages(turns, budget)\n"
            " context.append(selected)\n"
        )
        * 140,
        "paths_ids": "\n".join(
            "C:\\Users\\vandy\\ai-sessions\\sessions\\"
            f"{uuid.UUID(int=index)}\\rollout-{index:08x}.jsonl"
            for index in range(300)
        ),
        "tool_output": (
            "Script completed\nWall time 0.913 seconds\nOutput:\n"
            "FAILED tests/test_bridge.py::test_budget - AssertionError: 950000 != 712500\n"
        )
        * 180,
        "unicode": "Claude → Codex → OpenCode — résumé naïve 東京 🧪⚙️ “quoted” café\n" * 300,
        "dense_cjk": "漢字東京京都大阪技術開発人工知能会話履歴継続検証" * 600,
        "base64": b64encode(bytes(range(256)) * 80).decode("ascii"),
        "git_hashes": "\n".join(
            sha1(str(index).encode("ascii"), usedforsecurity=False).hexdigest()
            for index in range(800)
        ),
        "minified_json": json.dumps(
            [
                {"sha": f"{index:040x}", "ok": index % 2 == 0, "values": list(range(12))}
                for index in range(240)
            ],
            separators=(",", ":"),
        ),
    }


def main() -> None:
    encoding = tiktoken.get_encoding("o200k_base")
    ratios: list[float] = []
    for name, sample in corpus().items():
        tokens = len(encoding.encode(sample))
        ratio = len(sample) / tokens
        ratios.append(ratio)
        print(
            f"{name:12} chars={len(sample):7} tokens={tokens:7} "
            f"chars/token={ratio:.3f}"
        )
    print(
        f"min={min(ratios):.3f} median={statistics.median(ratios):.3f} "
        f"max={max(ratios):.3f} tiktoken={tiktoken.__version__} encoding=o200k_base"
    )


if __name__ == "__main__":
    main()
