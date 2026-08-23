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
    p10 = statistics.quantiles(ratios, n=10, method="inclusive")[0]
    print(
        f"min={min(ratios):.3f} p10={p10:.3f} "
        f"median={statistics.median(ratios):.3f} max={max(ratios):.3f}"
    )


if __name__ == "__main__":
    main()
