# Encoding Policy

## Context

Aemeath Agent uses UTF-8 for source code, tests, docs, YAML, and Markdown files.
Several Windows PowerShell views may display valid UTF-8 Chinese as mojibake when
the console output encoding is not UTF-8. Treat the file bytes, not the terminal
rendering, as the source of truth.

## How To Check

Use an explicit UTF-8 read when checking Chinese text in PowerShell:

```bash
python -c "from pathlib import Path; print(Path('docs/task_log.md').read_text(encoding='utf-8')[:200])"
```

Run the automated encoding and behavior tests:

```bash
pytest tests/test_chinese_encoding_and_behavior.py
```

Run the full release checks:

```bash
pytest
ruff check .
```

## Rules

- Save source code, tests, docs, and YAML as UTF-8.
- Do not trust `Get-Content` output alone when Chinese appears garbled in a Windows console.
- Do not commit text that contains UTF-8-as-GBK mojibake fragments such as the examples listed
  in `tests/test_chinese_encoding_and_behavior.py`.
- New Chinese behavior branches should include tests with readable Chinese input and expected
  output.
- Large curated data files may contain arbitrary source text, but runtime code, tests, docs, and
  config should stay readable.

## Current Guardrail

`tests/test_chinese_encoding_and_behavior.py` scans project text files for known mojibake markers
and verifies the Chinese branches of:

- `MockChatModelClient`
- `MemoryWriteClassifier`
- `StateUpdateClassifier`

If this test fails, repair the affected file as UTF-8 text before adding more behavior changes.
