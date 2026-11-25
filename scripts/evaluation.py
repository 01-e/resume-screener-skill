"""Validate a single isolated assessment and compute the score from locked weights."""
from common import integer, keys, require, text


def verify(e, filename, batch_id, c, resume):
    keys(e, 'batch_id filename name phone email wechat years years_quote education latest_role verdict gates dimensions off_role_quote summary highlights risks gaps')
    require(e['batch_id'] == batch_id and e['filename'] == filename, '原文件名或批次不一致。')
    evidence = {}

    def check_quote(q, optional=False):
        if q is None and optional:
            return
        require(text(q) and q in resume, '证据不是本候选人原文中的连续片段。')

    for field in ('name', 'phone', 'email', 'wechat', 'education', 'latest_role'):
        check_quote(e[field], optional=True)
    years = e['years']
    require(years is None or (type(years) in (int, float) and 0 <= years <= 100), '工作年限应为 0–100 的数字或 null。')
    if years is None:
        require(e['years_quote'] is None, '未明确年限时 years 和 years_quote 均填 null。')
    else:
        check_quote(e['years_quote'])

    def items(field, defs):
        rows = e[field]
        require(type(rows) is list and len(rows) == len(defs), '评估项数目与锁定口径不一致。')
        for row in rows:
            keys(row, 'id status quote' if field == 'gates' else 'id level quote note')
        ids = [r['id'] for r in rows]
        require(all(text(i) for i in ids) and set(ids) == {r['id'] for r in defs}, '评估编号缺失、重复或不属于当前岗位。')
        return rows

    fails = unclear = False
    for row in items('gates', c['hard_gates']):
        status = row['status']
        require(status in ('满足', '不符', '不明'), '门槛状态不正确。')
        check_quote(row['quote'], status == '不明')
        fails |= status == '不符'
        unclear |= status == '不明'
        evidence[row['id']] = row['quote']
    weights = {r['id']: r['weight'] for r in c['dimensions']}
    score = 0
    for row in items('dimensions', c['dimensions']):
        level, q = row['level'], row['quote']
        require(level is None or integer(level, 0, 4), '维度等级应为 0~4 或 null。')
        require(text(row['note']), '请用人话说明该维度的依据。')
        if level is None:
            require('简历未提及' in row['note'], '未计分维度须说明简历未提及。')
            require(q is None, '缺证维度请用 null，不得虚构证据。')
            unclear = True
        else:
            check_quote(q)
            score += weights[row['id']] * level / 4
        evidence[row['id']] = q
    other = e['off_role_quote']
    check_quote(other, optional=True)
    if other is not None:
        evidence['OFF'] = other
    v = e['verdict']
    require(v in ('推荐', '待定', '不匹配'), '分档必须是推荐、待定或不匹配。')
    require(v != '不匹配' or fails or other is not None, '缺少明确硬伤，不得判为不匹配。')
    require(v != '推荐' or not (fails or unclear or other or score < c['recommend_min']), '有门槛冲突、证据缺口或未过推荐线，不能推荐。')
    require(other is None or v == '不匹配', '已确认投错岗位应归为不匹配；转岗证据含糊请勿使用 OFF。')

    def claim(x):
        keys(x, 'text refs')
        refs = x['refs']
        require(text(x['text']) and isinstance(refs, list) and refs and all(text(r) and r in evidence for r in refs), '每条判断都要引用本人的有效评估编号。')
        if any(evidence[r] is None for r in refs):
            require('简历未提及' in x['text'], '无证据之处必须明确写“简历未提及”。')
    claim(e['summary'])
    for field in ['highlights', 'risks', 'gaps']:
        require(type(e[field]) is list, '亮点、风险与缺口应为列表。')
        for x in e[field]:
            claim(x)
    if v == '不匹配':
        decisive = {r['id'] for r in e['gates'] if r['status'] == '不符'} | ({'OFF'} if other else set())
        require(decisive.intersection(e['summary']['refs']), '不匹配的核心评价需引用明确硬伤。')
    return score

BASICS = 'name phone email wechat years years_quote education latest_role'


def assess(data, filename, batch_id, rules, resume):
    """Expand the compact wire format only after binding this row to its own original."""
    import re
    require(isinstance(data, dict) and data.get('filename') == filename, '结果与当前原文件名不一致。')
    tier = data.get('tier')
    if tier == '不匹配':
        keys(data, 'name filename tier score reason')
        require(data['score'] is None, '极简档未逐维计分，score 必须为 null，不能捏造分数。')
        require(data['name'] is None or (text(data['name']) and data['name'] in resume), '姓名不是本人原文。')
        reason = data['reason']
        match = re.fullmatch(r'(H[1-9]\d*|OFF)：([^\r\n「」]*「([^\r\n「」]{1,30})」[^\r\n「」]*)', reason) if text(reason) and len(reason) <= 40 else None
        require(match, '淘汰原因须 ≤40 字，引用一个 H 编号或 OFF 及「本人原文短引」。')
        ref, comment, quote = match.groups()
        require(ref == 'OFF' or ref in {r['id'] for r in rules['hard_gates']}, '淘汰原因未引用已确认的硬门槛。')
        require(text(quote) and quote in resume, '淘汰依据不属于本人的连续原文，请复核。')
        require(not any(word in comment for word in ('简历未提及', '未提到', '未写明', '未找到', '证据不足', '信息不足')),
                '不能以缺证作为淘汰原因，请复核待定档。')
        ev = dict.fromkeys(BASICS.split())
        ev.update(name=data['name'], filename=filename, verdict=tier, summary={'text': comment, 'refs': [ref]},
                  gates=[] if ref == 'OFF' else [{'id': ref, 'status': '不符', 'quote': quote}],
                  dimensions=[], off_role_quote=quote if ref == 'OFF' else None, highlights=[], risks=[], gaps=[])
        return None, ev

    require(tier in ('推荐', '待定'), '分档不正确。')
    keys(data, BASICS + ' filename tier gates dimensions summary highlights risks gaps')

    def unpack(row, fields):
        names = fields.split()
        require(type(row) is list and len(row) == len(names), '紧凑评估项字段数错误。')
        return dict(zip(names, row))

    ev = {k: data[k] for k in BASICS.split() + ['filename']}
    ev.update(batch_id=batch_id, verdict=tier, off_role_quote=None, summary=unpack(data['summary'], 'text refs'))
    for key, fields in [('gates', 'id status quote'), ('dimensions', 'id level quote note'),
                        ('highlights', 'text refs'), ('risks', 'text refs'), ('gaps', 'text refs')]:
        require(type(data[key]) is list, '评估项应为数组。')
        ev[key] = [unpack(r, fields) for r in data[key]]
    for q in [r['quote'] for r in ev['gates'] + ev['dimensions']] + [ev['years_quote']]:
        require(q is None or (text(q) and len(q) <= 30), '每条事实引句最多 30 字，必须是本人原文。')
    return verify(ev, filename, batch_id, rules, resume), ev
