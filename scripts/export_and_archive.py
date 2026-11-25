"""Verify one complete batch, publish offline HTML, Excel and copies only."""
import argparse
import hashlib
import shutil
import time
import zipfile
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from common import Problem, criteria, digest, disjoint, load, main_guard, require, timing
from evaluation import assess, verify
from microbatches import read_results
from report import MISSING, report_data, write_report

FOLDERS = {'推荐': '01_推荐', '待定': '02_待定', '不匹配': '03_不匹配', '异常': '04_异常需人工查看'}
BOOK = '简历筛选汇总表.xlsx'


def checked_batch(work):
    m = load(work/'manifest.json')
    require(isinstance(m, dict) and isinstance(m.get('source'), str) and Path(m['source']).is_absolute(),
            '批次清单不符合 V1.1，请按已确认口径重新解析到新批次。')
    require(digest(work/'criteria.json') == m['criteria_sha256'], '标准已改变，请重新确认并创建新批次。')
    c = criteria(load(work/'criteria.json'))
    names = [row['filename'] for row in m['candidates'] + m['anomalies']]
    require(names and all(isinstance(n, str) and Path(n).name == n and n not in ('', '.', '..') for n in names)
            and len(set(map(Path, names))) == len(names), '批次文件名异常或重复，请重新解析。')
    modern = m.get('version') == 2
    require(modern or ('version' not in m and 'microbatches' not in m), '不支持此批次版本，请重新解析。')
    if modern:
        wire = read_results(work, m, [row['filename'] for row in m['candidates']])
    else:  # Complete V1.1 work directories remain exportable with their original scoring rules.
        expected = {r['filename'] + '.json' for r in m['candidates']}
        require({p.name for p in (work/'evals').iterdir()} == expected, '评估目录有缺份或额外文件，请按 manifest 补齐；不会导出半套结果。')
    results = []
    for row in m['candidates']:
        name = row['filename']
        t = work/'texts'/f'{name}.txt'
        try:
            raw = t.read_bytes()
            require(hashlib.sha256(raw).hexdigest() == row['text_sha256'], '解析文本已改变，请重新解析。')
            resume = raw.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
            if modern:
                score, ev = assess(wire[name], name, m['batch_id'], c, resume)
            else:
                ev = load(work/'evals'/f'{name}.json')
                score = verify(ev, name, m['batch_id'], c, resume)
        except Problem as exc:
            raise Problem(f'{name}：{exc}') from None
        results.append((score, ev))
    return c, m, sorted(results, key=lambda v: (v[0] is None, -(v[0] or 0), v[1]['filename'].casefold(), v[1]['filename']))


def literal_row(ws, values):
    from openpyxl.cell import WriteOnlyCell
    row = []
    for v in values:
        if isinstance(v, str):
            cell = WriteOnlyCell(ws, value=v)
            cell.data_type = 's'  # Even a filename starting with '=' is literal data.
            row.append(cell)
        else:
            row.append(v)
    ws.append(row)


def create_book(root, c, data, results):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    main = wb.active
    main.title = '初筛排名'
    evidence = wb.create_sheet('逐项证据')
    anomalies = wb.create_sheet('异常清单')
    rules = wb.create_sheet('已确认口径')
    literal_row(main, ['排名', '分档', '综合得分', '候选人姓名', '手机', '邮箱', '微信', '工作年限', '学历', '最近职位', '一句话核心评价', '核心亮点', '风险提示', '待核实缺口', '原文件名'])
    literal_row(evidence, ['原文件名', '项目', '判定或等级', '原文引句', '维度得分（百分制）', '说明'])
    literal_row(anomalies, ['原文件名', '类型', '请人工查看的原因', '处理建议'])
    literal_row(rules, ['类别', '编号', '已确认内容', '权重（%）'])
    for label, value in [('岗位', c['role']), ('推荐参考线', c['recommend_min']), ('筛选时间', data['meta']['screening_time']),
                         ('批次来源', data['meta']['source_summary']), ('扫描数', data['meta']['total_files_scanned']),
                         ('有效解析数', data['meta']['valid_parsed'])]:
        literal_row(rules, [label, '', value, None])
    for value in c['hidden_requirements']:
        literal_row(rules, ['HR 补充条件', '', value, None])
    for group, label in [('hard_gates', '硬门槛'), ('dimensions', '评估项')]:
        for r in c[group]:
            literal_row(rules, [label, r['id'], r['text'], r.get('weight')])

    def render(claim):
        return claim['text'] + ' 〔' + '、'.join(claim['refs']) + '〕'
    for rank, (score, ev) in enumerate(results, 1):
        name = ev['filename']
        absent = '本档未展开' if score is None else MISSING
        literal_row(main, [rank, ev['verdict'], score if score is not None else '未评分', *[ev[k] if ev[k] is not None else absent
                           for k in ('name', 'phone', 'email', 'wechat', 'years', 'education', 'latest_role')],
                           render(ev['summary']), *['\n'.join(map(render, ev[k])) for k in ('highlights', 'risks', 'gaps')], name])
        for r in ev['gates'] + ev['dimensions']:
            level = r.get('status', r.get('level'))
            literal_row(evidence, [name, r['id'], '不明' if level is None else level, r['quote'] or MISSING,
                                   r['level'] * 25 if r.get('level') is not None else None, r.get('note')])
        if ev['off_role_quote']:
            literal_row(evidence, [name, 'OFF', '明确投错岗位', ev['off_role_quote']])
        if ev['years_quote']:
            literal_row(evidence, [name, '工作年限', ev['years'], ev['years_quote']])
    for row in data['abnormal_files']:
        literal_row(anomalies, [row[k] for k in ('filename', 'file_type', 'reason', 'suggestion')])
    wrap = Alignment(vertical='top', wrap_text=True)
    for ws, widths in [(main, [8, 12, 12, 20, 22, 32, 20, 14, 28, 30, 54, 48, 48, 48, 40]),
                       (evidence, [40, 16, 18, 85, 20, 48]), (anomalies, [40, 12, 56, 56]), (rules, [18, 12, 70, 15])]:
        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions
        for cell in ws[1]:
            cell.fill = PatternFill('solid', fgColor='195968')
            cell.font = Font(bold=True, color='FFFFFF')
        for i, width in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = width
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = wrap
    wb.save(root/BOOK)
    wb.close()


def build(root, source, c, m, results):
    data = report_data(c, m, results)
    for folder in FOLDERS.values():
        (root/folder).mkdir(parents=True)
    routes = {ev['filename']: ev['verdict'] for _, ev in results}
    with timing('export.archive'):
        for info in m['candidates'] + m['anomalies']:
            name = info['filename']
            tier = routes.get(name, '异常')
            src = source/name
            require(not src.is_symlink() and src.is_file() and src.resolve().parent == source, f'{name} 原件缺失或已替换，请核对输入；未交付。')
            dest = root/FOLDERS[tier]/name
            shutil.copy2(src, dest)
            require(digest(dest) == info['sha256'], f'{name} 原件在解析后有变更，请重新解析，不交付旧判断。')
    with timing('export.excel'):
        create_book(root, c, data, results)
    with timing('export.html'):
        write_report(root, data)


def retry_file_lock(action):
    # Windows indexers may hold a just-written attachment open for a moment.
    # Same bounded retry budget, but avoid sleeping 1–2 s after a lock has cleared.
    deadline = time.monotonic() + 3.85
    delay = 0.05
    while True:
        try:
            return action()
        except OSError as exc:
            left = deadline - time.monotonic()
            if getattr(exc, 'winerror', None) not in (5, 32, 145) or left <= 0:
                raise
            time.sleep(min(delay, left))
            delay = min(0.15, delay * 2)


def publish(target, zipped, source, work, data):
    require(disjoint(target, source) and disjoint(target, work), '交付位置须与输入、临时批次分开。')
    require(not target.exists(), '交付位置已存在，请指定新名称，避免覆盖历史结果。')
    target.parent.mkdir(parents=True, exist_ok=True)
    # Same filesystem staging: a copy/validation failure cannot expose a partial delivery.
    stage = TemporaryDirectory(prefix='.resume-deliver-', dir=target.parent)
    tmp = Path(stage.name)
    published = False
    try:
        root = tmp/'payload'
        build(root, source, *data)
        artifact = root
        if zipped:
            artifact = tmp/'result.zip'
            with zipfile.ZipFile(artifact, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as z:
                for p in sorted(root.rglob('*')):
                    z.write(p, p.relative_to(root))
        require(not target.exists(), '交付位置刚被占用，请换用新名称。')
        with timing('export.publish'):
            retry_file_lock(lambda: artifact.rename(target))
        published = True
    finally:
        try:
            retry_file_lock(stage.cleanup)
        except OSError:
            if not published:
                raise
            print(f'交付已完成，暂存目录被占用，请在本地清理：{tmp}')


def run(work, output, zipped=False, fallback_dir=None):
    work = work.resolve()
    with timing('export.verify'):
        data = checked_batch(work)
    _, m, results = data
    source = Path(m['source']).resolve()
    zipname = output.name if output.suffix.lower() == '.zip' else output.name + '.zip'
    target = (output.with_name(zipname) if zipped else output).absolute()
    try:
        publish(target.resolve(), zipped, source, work, data)
    except OSError:
        target = (fallback_dir or work.parent).resolve()/zipname
        print('常规交付位置不可用；尝试在可写工作区生成完整 ZIP。')
        publish(target, True, source, work, data)
    counts = Counter(ev['verdict'] for _, ev in results)
    print(f"已交付：{target}；推荐 {counts['推荐']}、待定 {counts['待定']}、不匹配 {counts['不匹配']}、异常 {len(m['anomalies'])}。原件未移动或删除。")
    return target


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--zip', action='store_true')
    p.add_argument('--fallback-dir', type=Path, help='无法写入目标时的可下载目录；默认为临时批次的父目录。')
    a = p.parse_args()
    with timing('export'):
        run(a.work, a.output, a.zip, a.fallback_dir)


if __name__ == '__main__':
    main_guard(main)