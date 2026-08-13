"""日志落盘不能把密钥带出去

loguru 默认 diagnose=True，异常回溯里会渲染每个变量的**值**。
okx_rest 构造请求头那一帧里就摆着 secret_key 和 passphrase，
一次异常就够把实盘密钥明文写进保留 30 天的日志文件。
"""
import re
from pathlib import Path

import pytest
from loguru import logger

SECRET = "SECRET-must-not-appear"
PASSPHRASE = "PASS-must-not-appear"


class _FakeClient:
    """复刻 okx_rest 的形状：失败行上直接引用持有密钥的属性。"""

    def __init__(self):
        self._secret_key = SECRET
        self._passphrase = PASSPHRASE

    def headers(self, body):
        return {"SIGN": self._secret_key.encode() + body,
                "PASSPHRASE": self._passphrase}


def _write_crash_log(path: Path, **sink_kwargs) -> str:
    logger.remove()
    sink = logger.add(str(path), level="DEBUG", **sink_kwargs)
    try:
        try:
            _FakeClient().headers("str-not-bytes")   # TypeError
        except Exception:
            logger.exception("签名失败")
    finally:
        logger.remove(sink)
    return path.read_text(encoding="utf-8")


def test_diagnose_would_leak_secrets(tmp_path):
    """先证明风险是真的：默认参数下密钥确实进了文件"""
    text = _write_crash_log(tmp_path / "leaky.log", diagnose=True)
    assert SECRET in text, "前提变了——loguru 不再渲染变量值，本测试需重写"


def test_diagnose_off_keeps_secrets_out(tmp_path):
    text = _write_crash_log(tmp_path / "safe.log", backtrace=True, diagnose=False)
    assert SECRET not in text
    assert PASSPHRASE not in text
    # 排障信息本身要留下：异常类型、调用链、出错的文件与行号
    assert "TypeError" in text
    assert "headers" in text


def test_main_configures_both_sinks_without_diagnose():
    """main.py 的两个 sink 都必须显式关掉 diagnose"""
    src = Path("main.py").read_text(encoding="utf-8")
    setup = src[src.index("def _setup_logging"):src.index("async def main")]

    adds = re.findall(r"logger\.add\((.*?)\n    \)", setup, re.S)
    assert len(adds) == 2, f"期望终端 + 文件两个 sink，实得 {len(adds)}"
    assert any("sys.stderr" in a for a in adds), "缺少终端输出"
    assert any("logs/" in a for a in adds), "缺少文件输出"
    for a in adds:
        assert "diagnose=False" in a, f"这个 sink 没关 diagnose：{a[:60]}"


@pytest.mark.parametrize("field", ["rotation", "retention", "compression"])
def test_file_sink_rotates_and_expires(field):
    """日志无限增长会吃满磁盘，而磁盘满会让策略在下单途中崩掉"""
    src = Path("main.py").read_text(encoding="utf-8")
    setup = src[src.index("def _setup_logging"):src.index("async def main")]
    assert field in setup
