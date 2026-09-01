# Benchmark Report: run-20260901-062229

## Executive Summary
- **Terminal Route**: `KEEP_LEXICAL`
- **Selected Winner**: `H2_LEXICAL_CORROBORATION`
- **Benchmark State**: `VALID`
- **Model ID**: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
- **Seed**: `42`
- **ADR Reference**: `ADR-053`
- **Runtime Identity**: `cpython-3.10.20-Windows-AMD64`

## Hashes & Authority
- **Locked Fixture**: `6e96049e76e471549b73d1606e714a888ee1f4ba64729423b6913739db5a435f`
- **Calibration Fixture**: `1d262ab3b3daa275e9bee166e024ae2f91c8d6d8a3fe95b993d2bb06feee9472`
- **Model Artifact**: `7cce6bd3df3eefe20757261165563b9f27b2397b5f0661bd42f804d545b07569`
- **Shared Config**: `c0d7110f0abe8dfae5e335dc1d931b4742c52e37ea67de296afbb22060912c43`

## Candidate Results Summary
| Candidate | Status | Quality | Hard Recall@1 | Syn MRR@3 | Para MRR@3 | Semantic Score | Retention vs B | Gain vs A |
| :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `H1_MARGIN` | EVALUABLE | CRITICAL_REGRESSION | 1.0000 | 0.8571 | 0.7188 | 0.7879 | 0.9388 | 0.7879 |
| `H2_LEXICAL_CORROBORATION` | EVALUABLE | QUALIFIED | 1.0000 | 0.0000 | 0.1562 | 0.0781 | 0.0931 | 0.0781 |
| `H4_ASYMMETRIC_GATE` | EVALUABLE | CRITICAL_REGRESSION | 1.0000 | 0.8571 | 0.7812 | 0.8192 | 0.9761 | 0.8192 |

## Architectural Recommendation
Based on the evaluated evidence, the lifecycle concluded with **`KEEP_LEXICAL`**.

