"""Shared test setup: put the repo root on sys.path and stub AWS SDK modules.

Lets the tests run offline without AWS credentials or boto3 installed.
"""
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

if importlib.util.find_spec("boto3") is None:
    stub = types.ModuleType("boto3")
    stub.client = lambda *args, **kwargs: None
    sys.modules["boto3"] = stub


def load_handler(name: str):
    """Import lambda_<name>/handler.py under a unique module name."""
    path = os.path.join(ROOT, f"lambda_{name}", "handler.py")
    spec = importlib.util.spec_from_file_location(f"{name}_handler", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
