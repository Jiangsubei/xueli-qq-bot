from __future__ import annotations

import tempfile
import unittest

from src.core.models import FactEvidenceRecord
from src.memory.storage.fact_evidence_store import FactEvidenceStore


class FactEvidenceStoreTests(unittest.TestCase):
    def test_records_and_signals_can_be_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = FactEvidenceStore(temp_dir)
            record = store.add_record(
                FactEvidenceRecord(
                    record_id="",
                    user_id="42",
                    source_memory_id="mem-1",
                    source_memory_type="ordinary",
                    decision_level="high_confidence",
                    confidence=0.88,
                    action="prefer_new",
                    conflict_type="factual_correction",
                    summary="用户现在改成不喝咖啡了",
                    reason="新近表述更明确",
                )
            )
            signal = store.build_signal(user_id="42", record=record, ttl_hours=24)
            records = store.list_records("42", memory_id="mem-1")
            signals = store.get_active_signals("42")

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].source_memory_id, "mem-1")
            self.assertEqual(len(signals), 1)
            self.assertEqual(signals[0].signal_id, signal.signal_id)


if __name__ == "__main__":
    unittest.main()
