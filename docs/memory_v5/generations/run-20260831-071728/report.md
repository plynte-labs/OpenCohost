# Memory v5 Semantic Retrieval Quality Benchmark Report

- **Run ID**: `run-20260831-071728`
- **State**: `VALID`
- **Terminal Route**: `KEEP_LEXICAL`
- **Winner**: `None`
- **Schema Version**: `memory-semantic-benchmark-receipt-v1`
- **Model ID**: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
- **ADR Reference**: `ADR-053`

## Hashes and Identity

- `locked_fixture_hash`: `6e96049e76e471549b73d1606e714a888ee1f4ba64729423b6913739db5a435f`
- `calibration_fixture_hash`: `1d262ab3b3daa275e9bee166e024ae2f91c8d6d8a3fe95b993d2bb06feee9472`
- `model_artifact_hash`: `7cce6bd3df3eefe20757261165563b9f27b2397b5f0661bd42f804d545b07569`
- `shared_config_hash`: `c0d7110f0abe8dfae5e335dc1d931b4742c52e37ea67de296afbb22060912c43`
- `lock_identity`: `cpython-3.10.20-Windows-AMD64`

## Candidates

- `candidates.b.variant_key`: `b`
- `candidates.b.execution_status`: `EVALUABLE`
- `candidates.b.execution_reason`: `None`
- `candidates.b.quality_status`: `CRITICAL_REGRESSION`
- `candidates.b.critical_reasons`: `NO_MEMORY_INJECTION`
- `candidates.b.calibration.threshold`: `0.21735759633642904`
- `candidates.b.calibration.frozen_at`: `2026-08-31T07:17:37Z`
- `candidates.b.calibration.frozen_sequence`: `1`
- `candidates.b.calibration.calibration_fixture_hash`: `1d262ab3b3daa275e9bee166e024ae2f91c8d6d8a3fe95b993d2bb06feee9472`
- `candidates.b.calibration.model_artifact_hash`: `7cce6bd3df3eefe20757261165563b9f27b2397b5f0661bd42f804d545b07569`
- `candidates.b.calibration.shared_config_hash`: `c0d7110f0abe8dfae5e335dc1d931b4742c52e37ea67de296afbb22060912c43`
- `candidates.b.calibration.cal_no_memory_abstention_count`: `4`
- `candidates.b.calibration.cal_no_memory_false_injection_count`: `0`
- `candidates.b.calibration.cal_profile_isolation_pass_count`: `4`
- `candidates.b.calibration.cal_profile_privacy_leakage_count`: `0`
- `candidates.b.calibration.cal_hard_correct_at_1_count`: `4`
- `candidates.b.calibration.cal_no_memory_abstention_accuracy`: `1.0`
- `candidates.b.calibration.cal_syn_para_mrr3_mean`: `0.875`
- `candidates.b.calibration.cal_hard_recall_at_1`: `1.0`
- `candidates.b.calibration.cal_syn_mrr_at_3`: `1.0`
- `candidates.b.calibration.cal_para_mrr_at_3`: `0.75`
- `candidates.c.variant_key`: `c`
- `candidates.c.execution_status`: `EVALUABLE`
- `candidates.c.execution_reason`: `None`
- `candidates.c.quality_status`: `CRITICAL_REGRESSION`
- `candidates.c.critical_reasons`: `NO_MEMORY_INJECTION`
- `candidates.c.calibration.threshold`: `SELECT_ALL`
- `candidates.c.calibration.frozen_at`: `2026-08-31T07:17:54Z`
- `candidates.c.calibration.frozen_sequence`: `1`
- `candidates.c.calibration.calibration_fixture_hash`: `1d262ab3b3daa275e9bee166e024ae2f91c8d6d8a3fe95b993d2bb06feee9472`
- `candidates.c.calibration.model_artifact_hash`: `7cce6bd3df3eefe20757261165563b9f27b2397b5f0661bd42f804d545b07569`
- `candidates.c.calibration.shared_config_hash`: `c0d7110f0abe8dfae5e335dc1d931b4742c52e37ea67de296afbb22060912c43`
- `candidates.c.calibration.cal_no_memory_abstention_count`: `4`
- `candidates.c.calibration.cal_no_memory_false_injection_count`: `0`
- `candidates.c.calibration.cal_profile_isolation_pass_count`: `4`
- `candidates.c.calibration.cal_profile_privacy_leakage_count`: `0`
- `candidates.c.calibration.cal_hard_correct_at_1_count`: `4`
- `candidates.c.calibration.cal_no_memory_abstention_accuracy`: `1.0`
- `candidates.c.calibration.cal_syn_para_mrr3_mean`: `0.875`
- `candidates.c.calibration.cal_hard_recall_at_1`: `1.0`
- `candidates.c.calibration.cal_syn_mrr_at_3`: `1.0`
- `candidates.c.calibration.cal_para_mrr_at_3`: `0.75`

## Metrics

- `metrics.a.hard_correct_at_1_count`: `16`
- `metrics.a.hard_correct_at_3_count`: `16`
- `metrics.a.syn_correct_at_1_count`: `0`
- `metrics.a.para_correct_at_1_count`: `0`
- `metrics.a.no_memory_abstention_count`: `12`
- `metrics.a.near_wrong_correct_at_1_count`: `7`
- `metrics.a.profile_isolation_pass_count`: `0`
- `metrics.a.stale_correct_count`: `2`
- `metrics.a.hard_recall_at_1`: `1.0`
- `metrics.a.syn_recall_at_1`: `0.0`
- `metrics.a.para_recall_at_1`: `0.0`
- `metrics.a.semantic_quality_score`: `0.0`
- `metrics.a.no_memory_abstention_accuracy`: `1.0`
- `metrics.a.near_wrong_precision_at_1`: `0.5833333333333334`
- `metrics.a.stale_correct_rate`: `0.4`
- `metrics.b.hard_correct_at_1_count`: `16`
- `metrics.b.hard_correct_at_3_count`: `16`
- `metrics.b.syn_correct_at_1_count`: `13`
- `metrics.b.para_correct_at_1_count`: `11`
- `metrics.b.no_memory_abstention_count`: `11`
- `metrics.b.near_wrong_correct_at_1_count`: `11`
- `metrics.b.profile_isolation_pass_count`: `5`
- `metrics.b.stale_correct_count`: `2`
- `metrics.b.hard_recall_at_1`: `1.0`
- `metrics.b.syn_recall_at_1`: `0.9285714285714286`
- `metrics.b.para_recall_at_1`: `0.6875`
- `metrics.b.semantic_quality_score`: `0.8392857142857143`
- `metrics.b.no_memory_abstention_accuracy`: `0.9166666666666666`
- `metrics.b.near_wrong_precision_at_1`: `0.9166666666666666`
- `metrics.b.stale_correct_rate`: `0.4`
- `metrics.c.hard_correct_at_1_count`: `16`
- `metrics.c.hard_correct_at_3_count`: `16`
- `metrics.c.syn_correct_at_1_count`: `13`
- `metrics.c.para_correct_at_1_count`: `11`
- `metrics.c.no_memory_abstention_count`: `11`
- `metrics.c.near_wrong_correct_at_1_count`: `11`
- `metrics.c.profile_isolation_pass_count`: `5`
- `metrics.c.stale_correct_count`: `3`
- `metrics.c.hard_recall_at_1`: `1.0`
- `metrics.c.syn_recall_at_1`: `0.9285714285714286`
- `metrics.c.para_recall_at_1`: `0.6875`
- `metrics.c.semantic_quality_score`: `0.8392857142857143`
- `metrics.c.no_memory_abstention_accuracy`: `0.9166666666666666`
- `metrics.c.near_wrong_precision_at_1`: `0.9166666666666666`
- `metrics.c.stale_correct_rate`: `0.6`
