import re
import unittest
from dataclasses import replace

from ai_sessions.discovery import MAX_EVIDENCE_IDS, EvidenceAccumulator, HarnessContext
from ai_sessions.registry import REGISTRY


class EvidenceAccumulatorTests(unittest.TestCase):
    def test_overlapping_patterns_preserve_first_byte_scan_order(self) -> None:
        evidence = EvidenceAccumulator((re.compile(rb"id-[0-9]+"), re.compile(rb"id-2")))
        evidence.scan(b"before id-2 then id-1 and id-2 again")
        self.assertEqual(evidence.tokens, ["id-2", "id-1"])

    def test_distinct_evidence_is_capped_and_disclosed(self) -> None:
        evidence = EvidenceAccumulator((re.compile(rb"x[0-9]{4}"),))
        line = " ".join(f"x{index:04d}" for index in range(MAX_EVIDENCE_IDS + 1)).encode()
        evidence.scan(line)
        self.assertEqual(len(evidence.tokens), MAX_EVIDENCE_IDS)
        self.assertEqual(evidence.tokens[-1], "x4095")
        self.assertTrue(evidence.truncated)

    def test_pattern_signature_changes_with_registry_generation(self) -> None:
        before = HarnessContext.create().pattern_signature
        base = REGISTRY.get("codex")
        fake = replace(base, name="patterned", id_patterns=(re.compile(rb"new-[0-9]+"),))
        with REGISTRY.temporary(fake):
            during = HarnessContext.create().pattern_signature
        after = HarnessContext.create().pattern_signature
        self.assertNotEqual(during, before)
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
