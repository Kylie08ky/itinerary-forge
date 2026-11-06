"""文档体检（scripts/check_docs.py）：断链、不存在的路径、孤立文档、规格里的进度状态行。"""

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_docs", Path(__file__).resolve().parent.parent / "scripts" / "check_docs.py"
)
check_docs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_docs)


def test_docs_have_no_dead_links_or_stale_status():
    problems = check_docs.run()
    assert not problems, "\n".join(problems)
