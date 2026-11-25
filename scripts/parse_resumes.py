"""Parse one confirmed job into a fresh, private local batch (no network)."""
import argparse
import hashlib
import os
import re
import uuid
from concurrent.futures import ProcessPoolExecutor
from io import BytesIO
from pathlib import Path
from importlib.util import find_spec

from microbatches import make_batches

from common import Problem, criteria, digest, disjoint, load, main_guard, require, save, timing, write_hashed_text


def extract_pdf(data):
    import pymupdf
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    with pymupdf.open(stream=data, filetype='pdf') as doc:
        if doc.needs_pass:
            raise Problem('PDF 已加密，无法提取文字。')
        return '\f'.join('\n'.join(b[4] for b in page.get_text('blocks', sort=True) if b[6] == 0) for page in doc)


def extract_docx(data):
    from docx import Document
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.text.paragraph import Paragraph
    d = Document(BytesIO(data))
    # Iterate paragraph XML in physical order, including every cell/nested table, without
    # duplicating merged cells. Include existing headers/footers without creating new parts.
    related = [r.target_part.element for r in d.part.rels.values() if r.reltype in (RT.HEADER, RT.FOOTER)]
    parts = [d.element.body, *related]
    return '\n'.join(Paragraph(p, d).text for root in parts for p in root.iter(qn('w:p')))


def parse_one(source):
    ext = source.suffix.lower()
    row = {'filename': source.name}

    def abnormal(reason, suggestion):
        row.update(reason=reason, suggestion=suggestion, file_type=ext[1:].upper() or '无扩展名')
        return row, None

    if ext not in ('.pdf', '.docx'):
        row['sha256'] = digest(source)
        return abnormal('不支持此文件格式。', '请提供可选中文字的 PDF/DOCX。')
    data = source.read_bytes()
    row['sha256'] = hashlib.sha256(data).hexdigest()
    try:
        raw = extract_pdf(data) if ext == '.pdf' else extract_docx(data)
    except ImportError:
        raise
    except Problem as e:
        return abnormal(str(e), '请联系候选人提供未加密副本。')
    except Exception:
        return abnormal('文件损坏或无法解析。', '请人工查看并重新导出。')
    if len(re.findall(r'[^\W_]', raw)) < 50:
        return abnormal('有效文字不足 50 字，疑似扫描件或图片简历。', '请人工打开查看，或索要可选中文字的电子版。')
    return row, raw


def auto_workers(files):
    # On Windows a spawn has to import the parsers afresh. For small local batches
    # the measured startup cost outweighs parallel parsing. Explicit --workers wins.
    if os.name == 'nt' and len(files) <= 32 and sum(f.stat().st_size for f in files) <= 1024 * 1024:
        return 1
    return min(4, len(files), os.cpu_count() or 1)


def run(source, rules, work, workers):
    c = criteria(load(rules))
    source, work = source.resolve(), work.resolve()
    require(source.is_dir(), '找不到输入文件夹，请共享一个可读的本地目录。')
    require(disjoint(source, work) and source not in rules.resolve().parents, '标准与临时批次必须放在简历输入目录以外。')
    require(not work.exists(), '临时批次已存在，请使用新的空闲路径，避免混入旧候选人。')
    files = []
    skipped_dirs = 0
    for f in sorted(source.iterdir(), key=lambda p: (p.name.casefold(), p.name)):
        require(not f.is_symlink() and not getattr(f, 'is_junction', lambda: False)(), '输入含链接，请移出链接，改放真实简历副本后重试。')
        if f.is_dir():
            skipped_dirs += 1
        else:
            require(f.is_file(), '输入含非普通文件，请只放入本地文件副本。')
            files.append(f)
    require(files, '输入目录顶层没有文件，请将本岗位待筛文件放在同一层。')
    if workers is None:
        workers = auto_workers(files)
    require(1 <= workers <= 16, '并发数应为 1 到 16。')
    # Detect missing required libraries once, not as dozens of apparently corrupt files.
    if not all(find_spec(pkg) for pkg in ('pymupdf', 'docx')):
        raise ImportError('missing local parser')
    work.mkdir(mode=0o700, parents=True)
    (work/'texts').mkdir()
    (work/'evals').mkdir()
    save(work/'criteria.json', c)
    batch = uuid.uuid4().hex
    m = {'version': 2, 'batch_id': batch, 'source': str(source), 'criteria_sha256': digest(work/'criteria.json'), 'candidates': [], 'anomalies': [], 'skipped_dirs': skipped_dirs}
    with timing('parse.extract'):
        pool = ProcessPoolExecutor(min(workers, len(files))) if workers > 1 and len(files) > 1 else None
        try:
            for row, raw in (pool.map(parse_one, files) if pool else map(parse_one, files)):
                if raw is None:
                    m['anomalies'].append(row)
                else:
                    target = work/'texts'/(row['filename'] + '.txt')
                    row['text_sha256'] = write_hashed_text(target, raw)
                    m['candidates'].append(row)
        finally:
            if pool:
                pool.shutdown(cancel_futures=True)
    with timing('parse.pack'):
        m['microbatches'] = make_batches(work, m, c)
    save(work/'manifest.json', m)  # last: existence signals a complete batch
    print(f"解析完成（解析进程 {workers}）：可评估 {len(m['candidates'])} 份，微批 {len(m['microbatches'])} 个，异常 {len(m['anomalies'])} 份，未遍历子目录 {skipped_dirs} 个。")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--criteria', type=Path, required=True)
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--workers', type=int, help='1–16；不传时按平台和批次大小选择。')
    a = p.parse_args()
    with timing('parse'):
        run(a.input, a.criteria, a.work, a.workers)


if __name__ == '__main__':
    main_guard(main)