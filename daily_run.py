#!/usr/bin/env python3
"""
Backward-compatible daily pipeline runner.
Delegates to `main.py`.
"""
import sys
from main import run_pipeline_cli

if __name__ == "__main__":
    run_pipeline_cli()
