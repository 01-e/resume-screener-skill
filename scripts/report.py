"""Project one verified batch into the supplied offline report contract."""
import json
import re
from datetime import datetime
from pathlib import Path

from common import Problem, require

REPORT = '简历筛选报告.html'
TEMPLATE = Path(__file__).resolve().parents[1]/'assets'/'report_template.html'
STATUS = {'推荐': 'recommended', '待定': 'pending', '不匹配': 'rejected'}
MISSING = '简历未提及'


def report_data(c, m, results):
    candidates = []
    for score, ev in results:
        by_id = {r['id']: r for r in ev['dimensions']}
        dims = []
        for item in c['dimensions'] if score is not None else ():
            r = by_id[item['id']]
            level = r['level']
            dims.append({'name': item['text'], 'score': level * 25 if level is not None else 0,
                         'note': r['note'] if level is not None else r['note'] + '（此项未计分）'})
        quotes = [r['quote'] for r in ev['gates'] + ev['dimensions']] + [ev['off_role_quote'], ev['years_quote']]
        candidates.append({
            'name': ev['name'] or MISSING, 'phone': ev['phone'] or '', 'email': ev['email'] or '',
            'years': ev['years'], 'education': ev['education'] or (MISSING if score is not None else '本档未展开学历'),
            'latest_role': ev['latest_role'] or (MISSING if score is not None else '本档未展开职位'),
            'status': STATUS[ev['verdict']], 'score': score, 'filename': ev['filename'], 'comment': ev['summary']['text'],
            **{key: [claim['text'] for claim in ev[key]] for key in ('highlights', 'risks', 'gaps')},
            'dimensions': dims, 'evidence': list(dict.fromkeys(q for q in quotes if q))})
    abnormal = [{k: row[k] for k in ('filename', 'file_type', 'reason', 'suggestion')} for row in m['anomalies']]
    return {
        'meta': {'job_title': c['role'], 'screening_time': datetime.now().astimezone().isoformat(timespec='minutes'),
                 'total_files_scanned': len(candidates) + len(abnormal), 'valid_parsed': len(candidates),
                 'source_summary': '本地收件目录：' + Path(m['source']).name},
        'criteria': {'hard_requirements': [r['text'] for r in c['hard_gates']],
                     'hidden_requirements': c['hidden_requirements'],
                     'dimensions': [{'name': r['text'], 'weight': r['weight']} for r in c['dimensions']]},
        'candidates': candidates, 'abnormal_files': abnormal}


def write_report(root, data):
    require(TEMPLATE.is_file(), '缺少 assets/report_template.html，请安装完整 Skill。')
    # No newline translation: preserve every byte outside the JSON value, including CRLF.
    template = TEMPLATE.read_bytes().decode('utf-8')
    declarations = list(re.finditer(r'^const REPORT_DATA = ', template, re.MULTILINE))
    require(len(declarations) == 1, '报告模板必须有且仅有一处 REPORT_DATA 声明，未交付。')
    start = declarations[0].end()
    try:
        sample, end = json.JSONDecoder().raw_decode(template, start)
    except json.JSONDecodeError as exc:
        raise Problem('报告模板的 REPORT_DATA 不是合法 JSON，未交付。') from exc
    require(isinstance(sample, dict) and template.startswith(';', end),
            '报告模板必须直接以 JSON 对象给 REPORT_DATA 赋值并以分号结束，未交付。')

    payload = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    payload = re.sub(r'</(?=script\b)', lambda _: r'<\/', payload, flags=re.I)
    payload = payload.replace('<!', r'\u003c!').replace('\u2028', r'\u2028').replace('\u2029', r'\u2029')
    (root/REPORT).write_bytes((template[:start] + payload + template[end:]).encode('utf-8'))
