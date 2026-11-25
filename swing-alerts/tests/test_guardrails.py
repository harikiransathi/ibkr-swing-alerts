import inspect
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_no_order_code_anywhere():
    bad = re.compile(r"placeOrder|cancelOrder|reqGlobalCancel|LimitOrder|MarketOrder|StopOrder|StopLimitOrder|"
                     r"\bOrder\(|bracketOrder|whatIfOrder")
    for f in (ROOT / "alerts").glob("*.py"):
        hits = [ln for ln in f.read_text(encoding="utf-8").splitlines() if bad.search(ln)]
        assert not hits, f"{f.name} contains order code: {hits}"


def test_ib_connects_read_only():
    from alerts.data import IBData
    src = inspect.getsource(IBData.connect)
    assert "readonly=True" in src


def test_refuses_without_alerts_only(tmp_path):
    from alerts.scanner import Scanner
    with pytest.raises(SystemExit):
        Scanner({"alerts_only": False, "rules": {}, "files": {"state_dir": str(tmp_path)}}, dry_run=True)


def test_secrets_not_tracked():
    gi = (ROOT / ".gitignore").read_text()
    assert "config/secrets.yaml" in gi and "state/" in gi
