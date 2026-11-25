"""Model-facing views only: keep the original text as the source of evidence."""
import hashlib
import json
import re
import secrets
from pathlib import Path

from common import Problem, digest, keys, require, strict_object, write_hashed_text

LIMIT = 4000
PAGE = re.compile(r'(?:第\s*\d+\s*页(?:\s*[/共]\s*\d+\s*页?)?|page\s+\d+(?:\s*(?:of|/)\s*\d+)?)', re.I)
TERMS = ('大专', '本科', '学士', '硕士', '博士', 'PMP', 'CPA', '注册会计师', '教师资格证', '法律职业资格证')


def cleaned(raw):
    """Drop only decorative lines/page numbers; retain one copy of recurring edge text."""
    seen_edges = set()
    out = []
    for page in raw.split('\f'):
        lines = [s.strip() for s in page.splitlines() if s.strip()]
        for i, line in enumerate(lines):
            if PAGE.fullmatch(line) or re.fullmatch(r'[-=_─━\s]{3,}', line):
                continue
            # Headers/footers are often names or job titles: never remove the first occurrence.
            if i in (0, len(lines) - 1) and re.search(r'简历|履歴書|résumé|resume|curriculum vitae|confidential|保密', line, re.I) and len(line) <= 60:
                edge = (i == 0, line)
                if edge in seen_edges:
                    continue
                seen_edges.add(edge)
            out.append(line)
    return '\n'.join(out)


def groups(rows):
    """Balanced 3–5, except an entire input of just one or two readable files."""
    count = (len(rows) + 4) // 5
    if count:
        size, extra = divmod(len(rows), count)
        start = 0
        for i in range(count):
            end = start + size + (i < extra)
            yield rows[start:end]
            start = end


def block(index, filename, body):
    # Every untrusted line is quoted. A delimiter inside a CV is not a new candidate.
    return f'=== RESUME_ID: {index:03d} ===\nfilename: {json.dumps(filename, ensure_ascii=False)}\n' + body


def quoted(body):
    return '\n'.join('> ' + line for line in body.split('\n'))


def make_batches(work, manifest, rules):
    folder = work/'batches'
    folder.mkdir()
    gates = ' '.join(r['text'] for r in rules['hard_gates'])
    terms = [t for t in TERMS if t in gates] + re.findall(r'「([^「」\n]{1,30})」', gates)
    batches = []
    index = 0
    for n, rows in enumerate(groups(manifest['candidates']), 1):
        key = f'batch_{n:02d}'
        previews, supplements = [], []
        group = {'key': key, 'filenames': [r['filename'] for r in rows]}
        for row in rows:
            index += 1
            name = row['filename']
            raw = (work/'texts'/(name + '.txt')).read_text('utf8')
            view = cleaned(raw)
            hits = [term for term in dict.fromkeys(terms) if term in raw]
            hint = f'[快速提取] 字符数: {len(raw)} | 文件格式: {Path(name).suffix[1:].lower()}'
            if hits:
                hint += ' | 门槛字面命中（非结论）: ' + '、'.join(hits)
            if len(view) > LIMIT:
                head, tail = LIMIT * 3 // 5, LIMIT * 2 // 5
                supplements.append(block(index, name, quoted(view[head:-tail])))
                body = quoted(view[:head]) + f'\n[中段截断：定档前必读 {key}.more.md]\n' + quoted(view[-tail:])
            else:
                body = quoted(view)
            previews.append(block(index, name, hint + '\n' + body))
        group['text_sha256'] = write_hashed_text(folder/f'{key}.md', '\n\n'.join(previews))
        if supplements:
            token = secrets.token_hex(8)
            group['more_sha256'] = write_hashed_text(folder/f'{key}.more.md', '\n\n'.join(supplements) + '\nreview_token: ' + token)
            group['review_sha256'] = hashlib.sha256(token.encode()).hexdigest()
        batches.append(group)
    return batches


def read_results(work, manifest, names):
    """One private append-only JSONL shard per microbatch; never mix workers or jobs."""
    groups = manifest['microbatches']
    require(isinstance(groups, list) and [g['key'] for g in groups] == [f'batch_{i:02d}' for i in range(1, len(groups)+1)]
            and [f for g in groups for f in g['filenames']] == names, '微批清单与本批文件不一致。')
    expected = {g['key'] + '.jsonl' for g in groups}
    require({p.name for p in (work/'evals').iterdir()} == expected, '微批结果文件缺失或多余，不会交付半套结果。')
    results = {}
    for group in groups:
        key = group['key']
        require(digest(work/'batches'/f'{key}.md') == group['text_sha256'], f'{key}：微批文本已改变，请重新解析。')
        if 'review_sha256' in group:
            require(digest(work/'batches'/f'{key}.more.md') == group['more_sha256'], f'{key}：补页已改变，请重新解析。')
        with (work/'evals'/(key + '.jsonl')).open(encoding='utf-8-sig') as f:
            try:
                rows = [json.loads(line, object_pairs_hook=strict_object) for line in f if line.strip()]
            except (ValueError, UnicodeError) as exc:
                raise Problem(f'{key}：结果不是有效 JSONL；一行一个对象。') from exc
        require(rows, f'{key}：缺少批头。')
        head, *records = rows
        keys(head, 'batch_id batch' + (' review_token' if 'review_sha256' in group else ''))
        require(head['batch_id'] == manifest['batch_id'] and head['batch'] == key, f'{key}：批次不符。')
        if 'review_sha256' in group:
            token = head['review_token']
            require(isinstance(token, str) and hashlib.sha256(token.encode()).hexdigest() == group['review_sha256'],
                    f'{key}：请读完截断补页再定档。')
        found = [r.get('filename') if isinstance(r, dict) else None for r in records]
        require(all(isinstance(n, str) for n in found) and sorted(found) == sorted(group['filenames']),
                f'{key}：缺份、重复或混入别批文件。')
        results.update(zip(found, records))
    return results
