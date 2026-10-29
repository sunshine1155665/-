from __future__ import annotations

import json
import unittest
from pathlib import Path

from fatfish.security import sanitize_inbound, wrap_untrusted

CASES = Path(__file__).with_name("eval") / "injection.jsonl"


class InjectionEvalTests(unittest.TestCase):
    def test_injection_cases(self) -> None:
        failures = []
        for line in CASES.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            result = sanitize_inbound(case["input"])
            actual = "block" if result.risk >= 0.3 else "benign"
            critical_ok = (not case.get("critical")) or result.risk >= 0.85
            wrapped = wrap_untrusted(case["input"], source=case.get("channel", "external"))
            wrap_ok = 'trust="external"' in wrapped and wrapped.count("</untrusted_content>") == 1
            if actual != case["expect"] or not critical_ok or not wrap_ok:
                failures.append(
                    {
                        "id": case["id"],
                        "expect": case["expect"],
                        "actual": actual,
                        "risk": result.risk,
                        "critical_ok": critical_ok,
                        "wrap_ok": wrap_ok,
                    }
                )
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
