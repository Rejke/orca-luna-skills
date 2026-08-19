#!/usr/bin/env python3
"""Render compact Luna task prompts and dispatch Orca worker waves.

GENERATED FILE - do not edit directly. Source lives in scripts/parts/*.py;
rebuild with: python3 scripts/build_helper.py
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback keeps atomic writes.
    fcntl = None

