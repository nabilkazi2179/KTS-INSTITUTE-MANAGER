"""Vercel serverless entry point.

Vercel sets the VERCEL env var automatically, which switches app.py into
serverless mode: uploads and rate-limit state go to the database instead of
the local filesystem, which is wiped between invocations.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app import app as application  # noqa: E402

app = application
