# Usage report - final full-dataset run

Run that produced `output.csv`: split `eval`, 518 requests, 101.2 s wall time (`python code/main.py`).

| Provider | Model | Purpose | Calls | Cache hits | Input tokens | Output tokens | Cost (USD) |
|---|---|---|---|---|---|---|---|
| local (RapidOCR (PP-OCR ONNX)) | rapidocr-onnxruntime | screenshot OCR | 11 | 0 | 0 | 0 | 0.0000 |
| **Total** | | | **11** | | **0** | **0** | **0.0000** |

- Total tokens: 0; average tokens per request: 0.0
- Estimated total cost: $0.0000; per request: $0.000000
- Media extraction engines used: {'rapidocr': 11, 'pdfplumber': 4} (15 PDF/PNG evidence files; OCR results are cached by file hash, so repeated runs re-use them)

No remote model was called in this run: `ANTHROPIC_API_KEY` was not set, so intent classification used the deterministic multilingual rule classifier and screenshots used local RapidOCR (PP-OCR models on ONNX Runtime, CPU). The evidence reasoning (identity resolution, state reconstruction, policy, planning) is deterministic in every mode.

## Projected cost with Claude enabled (`--llm`)

Estimated, not measured (no API key was available while building): intent prompt ~330 input / ~90 output tokens per request; one screenshot (resized to <=1568 px, ~2,200 image tokens) ~2,300 input / ~700 output tokens. Prices: claude-opus-5-5 $4 / $20 per MTok.

| Model | Purpose | Calls | Input tokens | Output tokens | Est. cost (USD) |
|---|---|---|---|---|---|
| claude-opus-5-5 | intent classification | 518 | 170940 | 46620 | 1.6162 |
| claude-opus-5-5 | screenshot transcription | 11 | 25300 | 7700 | 0.2552 |
| **Total** | | **529** | **196240** | **54320** | **1.8714** |

Average 484 tokens and $0.00361 per request. Answers are cached, so re-runs cost nothing.
