"""Shared batch contracts. Diagnostics must not contain raw resume data."""
from contextlib import contextmanager
from time import perf_counter
import hashlib
import json
import re
import sys


class Problem(Exception):
    """A user-facing message, safe to display to the host."""


def require(condition, message):
    if not condition:
        raise Problem(message)


def strict_object(pairs):
    obj = dict(pairs)
    require(len(obj) == len(pairs), "JSON 有重复字段，请重新写入。")
    return obj


def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=strict_object)
    except (UnicodeError, json.JSONDecodeError):
        raise Problem("JSON 格式有误，请按结构约定写入 UTF-8 文件。") from None


def save(path, data):
    with path.open("x", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)


def write_hashed_text(path, text):
    """Hash the same UTF-8 bytes we write; later export still checks the on-disk file."""
    data = text.encode('utf-8')
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def keys(obj, names):
    require(type(obj) is dict and set(obj) == set(names.split()), "JSON 字段缺失或多余，请对照结构约定。")


def text(value):
    return isinstance(value, str) and bool(value.strip())


def integer(n, low, high):
    return type(n) is int and low <= n <= high


def criteria(obj):
    keys(obj, "role confirmed hard_gates hidden_requirements dimensions recommend_min")
    require(type(obj['hidden_requirements']) is list and all(text(r) for r in obj['hidden_requirements']),
            "HR 补充条件应为非空文字列表，没有补充时填 []。")
    require(text(obj['role']) and obj['confirmed'] is True, "请先与 HR 确认单岗位筛选口径，再锁定标准。")
    require(integer(obj['recommend_min'], 1, 100), "推荐线必须是 1 至 100 的整数。")
    for field, prefix in [('hard_gates', 'H'), ('dimensions', 'D')]:
        rows = obj[field]
        require(type(rows) is list, "门槛与评分项必须是列表。")
        ids = set()
        for row in rows:
            keys(row, 'id text' if prefix == 'H' else 'id text weight')
            cid = row['id']
            require(text(cid) and re.fullmatch(prefix + r'[1-9]\d*', cid) and cid not in ids and text(row['text']), "标准编号重复、格式错误或缺少判定口径。")
            ids.add(cid)
            if prefix == 'D':
                require(integer(row['weight'], 1, 100), "维度权重须为正整数。")
    require(sum(r['weight'] for r in obj['dimensions']) == 100, "评估维度权重之和须为 100。")
    return obj


def disjoint(a, b):
    return a != b and a not in b.parents and b not in a.parents


def main_guard(fn):
    try:
        fn()
    except Problem as exc:
        print(str(exc), file=sys.stderr)
    except ImportError:
        print("缺少解析或导出组件，请按 README 安装 requirements.txt 后重试。", file=sys.stderr)
    except FileNotFoundError:
        print("文件或目录不存在，请检查输入、批次状态及输出位置。", file=sys.stderr)
    except PermissionError:
        print("无法读写文件，请检查权限、文件占用或改用可写的沙箱交付目录。", file=sys.stderr)
    except OSError:
        print("本地文件操作失败，请检查磁盘空间、路径、权限后重试。", file=sys.stderr)
    except Exception:
        # Third-party exceptions can embed filenames or resume fragments in tracebacks.
        print("未能完成本批处理。请核对批次文件是否完整、组件是否可用；若仍失败请用合成样例排查，不要提交真实简历。", file=sys.stderr)
    else:
        return
    sys.exit(2)

@contextmanager
def timing(stage):
    start = perf_counter()
    try:
        yield
    finally:
        print(f'[TIMING] {stage}: {perf_counter() - start:.3f}s')
