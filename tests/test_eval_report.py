"""评测报告（docs/evaluation.md）必须和现在的数据一致：过期就重新跑 scripts/build_eval_report.py。"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("build_eval_report", ROOT / "scripts" / "build_eval_report.py")
report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report)


def test_evaluation_report_is_up_to_date():
    assert report.main.__module__  # 脚本可导入
    import sys
    argv, sys.argv = sys.argv, ["build_eval_report.py", "--check"]
    try:
        code = report.main()
    finally:
        sys.argv = argv
    assert code == 0, "docs/evaluation.md 已过期：运行 python scripts/build_eval_report.py 重新生成"
