#!/usr/bin/env python3
"""Record sys.argv[1:] + selected env to a JSON file, then exit 0.

Used by adversarial argv round-trip + wrapper-propagation tests to PROVE the
resolved argv arrives byte-for-byte (no shell interpretation). Invoked as:
    <interpreter.exe> argv_recorder.py [arg1 arg2 ...]
so sys.argv = ['argv_recorder.py', arg1, arg2, ...] and sys.argv[1:] = the args.
"""
import json, os, sys

out = os.environ.get("FRY_ARGV_OUT")
if out:
    try:
        with open(out, "w", encoding="utf-8") as f:
            rec = {
                "argv": sys.argv[1:],
                "env_is_environ": os.environ.__class__.__name__,
                "anthropic_base_url": os.environ.get("ANTHROPIC_BASE_URL", ""),
                "anthropic_model": os.environ.get("ANTHROPIC_MODEL", ""),
            }
            json.dump(rec, f, ensure_ascii=False)
    except Exception as e:
        sys.stderr.write(f"argv_recorder: write failed: {e}\n")
        sys.exit(4)
sys.exit(0)