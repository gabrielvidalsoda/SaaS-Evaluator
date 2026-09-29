"""Every module must import — several are loaded lazily by the CLI/pipeline, so a
syntax error there would otherwise only surface mid-run."""

from __future__ import annotations

import importlib
import pkgutil

import saas_eval


def test_all_modules_import():
    for mod in pkgutil.walk_packages(saas_eval.__path__, prefix="saas_eval."):
        importlib.import_module(mod.name)
