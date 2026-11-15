"""声明修订比较：区分资料修订与经营活动变化。

四阶段桥接（每条共同活动逐步替换输入）：
  S0 旧活动量 × 旧因子 × 旧分摊
  S1 旧活动量 × 新因子 × 旧分摊   → factor_effect（资料修订：因子依据更新）
  S2 旧活动量 × 新因子 × 新分摊   → allocation_effect（资料修订：分摊依据更新）
  S3 新活动量 × 新因子 × 新分摊   → activity_effect（经营活动变化）
新增/移除活动单独列入 operational 结构变化。因子重新挂接（活动换了 factor 引用）
属于经营/口径结构变化，不计入因子修订效应。
历史发布始终按各自 revision 的资料解释；本模块只做两个不可变版本之间的比较，
不会用新因子覆盖旧版本的任何数字。
"""
from .domain import emission_line, factor_mapping, summarize
from .uncertainty import analyze_uncertainty


def _r(value):
    return round(float(value), 9)


def _line_value(activity, factor, allocation=None):
    value, _ = emission_line(activity, factor)
    shares = activity['allocation'] if allocation is None else allocation
    return {product: value * share for product, share in shares.items()}


def compare_revisions(old_data, new_data):
    old_factors = factor_mapping(old_data)
    new_factors = factor_mapping(new_data)
    old_activities = {row['id']: row for row in old_data['activities']}
    new_activities = {row['id']: row for row in new_data['activities']}
    common_ids = sorted(set(old_activities) & set(new_activities))
    old_only = sorted(set(old_activities) - set(new_activities))
    new_only = sorted(set(new_activities) - set(old_activities))
    products = sorted(
        set().union(*(activity['allocation'].keys() for activity in old_data['activities'] + new_data['activities']))
        if old_data['activities'] or new_data['activities'] else set()
    )

    def blank_bridge():
        return {name: {product: 0.0 for product in products}
                for name in ('factor_effect', 'allocation_effect', 'activity_effect',
                             'added_effect', 'removed_effect', 'relink_effect')}

    bridge = blank_bridge()
    changelog = []

    def add_effect(bucket, values, sign=1.0):
        for product, value in values.items():
            bridge[bucket][product] += sign * value

    # 共同活动：四阶段分解
    for activity_id in common_ids:
        old_activity = old_activities[activity_id]
        new_activity = new_activities[activity_id]
        old_factor = old_factors.get(old_activity['factor'])
        new_factor = new_factors.get(new_activity['factor'])
        entry = {
            'activity_id': activity_id,
            'old_factor': old_activity['factor'],
            'new_factor': new_activity['factor'],
            'changes': [],
        }
        if old_activity['factor'] != new_activity['factor']:
            entry['changes'].append('factor_relink')
        if old_activity['amount'] != new_activity['amount'] or old_activity['unit'] != new_activity['unit']:
            entry['changes'].append('activity_amount')
        if old_activity['allocation'] != new_activity['allocation']:
            entry['changes'].append('allocation_basis')

        # 任一端缺因子：不参与中心值分解，按结构变化（旧链路移除、新链路新增）记录
        if old_factor is None or new_factor is None:
            entry['changes'].append('missing_factor_side')
            if old_factor is not None:
                add_effect('relink_effect', _line_value(old_activity, old_factor), sign=-1.0)
            if new_factor is not None:
                add_effect('relink_effect', _line_value(new_activity, new_factor))
            changelog.append(entry)
            continue

        relinked = old_activity['factor'] != new_activity['factor']
        s0 = _line_value(old_activity, old_factor)
        if not relinked:
            # S1：仅因子依据更新（活动量、分摊保持旧值）
            s1 = _line_value(old_activity, new_factor)
            for product in products:
                bridge['factor_effect'][product] += s1.get(product, 0.0) - s0.get(product, 0.0)
            # S2：分摊依据更新
            s2 = _line_value(old_activity, new_factor, new_activity['allocation'])
            for product in products:
                bridge['allocation_effect'][product] += s2.get(product, 0.0) - s1.get(product, 0.0)
            # S3：活动量变化（经营）
            s3 = _line_value(new_activity, new_factor)
            for product in products:
                bridge['activity_effect'][product] += s3.get(product, 0.0) - s2.get(product, 0.0)
        else:
            # 重新挂接视为经营/结构变化：旧链路整体移除、新链路整体新增
            add_effect('relink_effect', s0, sign=-1.0)
            add_effect('relink_effect', _line_value(new_activity, new_factor))
        changelog.append(entry)

    # 新增与移除活动
    added_logs, removed_logs = [], []
    for activity_id in new_only:
        activity = new_activities[activity_id]
        factor = new_factors.get(activity['factor'])
        log = {'activity_id': activity_id, 'changes': ['added_activity']}
        if factor is not None:
            add_effect('added_effect', _line_value(activity, factor))
        else:
            log['changes'].append('missing_factor')
        added_logs.append(log)
    for activity_id in old_only:
        activity = old_activities[activity_id]
        factor = old_factors.get(activity['factor'])
        log = {'activity_id': activity_id, 'changes': ['removed_activity']}
        if factor is not None:
            add_effect('removed_effect', _line_value(activity, factor), sign=-1.0)
        else:
            log['changes'].append('missing_factor')
        removed_logs.append(log)
    changelog.extend(added_logs)
    changelog.extend(removed_logs)

    # 因子层面的资料修订明细（即使没有活动引用也记录依据变化）
    factor_changes = []
    for factor_id in sorted(set(old_factors) | set(new_factors)):
        old_factor = old_factors.get(factor_id)
        new_factor = new_factors.get(factor_id)
        if old_factor is None:
            factor_changes.append({'factor_id': factor_id, 'change': 'added_factor',
                                   'detail': '新增因子依据，仅影响引用它的活动'})
        elif new_factor is None:
            factor_changes.append({'factor_id': factor_id, 'change': 'removed_factor',
                                   'detail': '因子依据移除，历史版本仍按旧依据解释'})
        else:
            fields = []
            if old_factor['kg_per_unit'] != new_factor['kg_per_unit']:
                fields.append('kg_per_unit')
            if old_factor['uncertainty'] != new_factor['uncertainty']:
                fields.append('uncertainty')
            if old_factor['source'] != new_factor['source']:
                fields.append('source')
            if old_factor['unit'] != new_factor['unit']:
                fields.append('unit')
            if fields:
                factor_changes.append({'factor_id': factor_id, 'change': 'revised_factor',
                                       'fields': fields})

    def total_of(values):
        return sum(values.values())

    old_summary = summarize(old_data)
    new_summary = summarize(new_data)
    old_summary_known = old_summary['known_kg']
    new_summary_known = new_summary['known_kg']

    effect_totals = {}
    for name, values in bridge.items():
        effect_totals[name] = _r(total_of(values))
    reconciled = old_summary_known + sum(effect_totals.values())
    reconciliation_ok = abs(reconciled - new_summary_known) < 1e-6

    product_bridge = []
    for product in products:
        product_bridge.append({
            'product': product,
            'old_kg': _r(sum(
                _line_value(activity, old_factors[activity['factor']]).get(product, 0.0)
                for activity in old_data['activities']
                if old_factors.get(activity['factor']) is not None
            )),
            'factor_effect_kg': _r(bridge['factor_effect'][product]),
            'allocation_effect_kg': _r(bridge['allocation_effect'][product]),
            'activity_effect_kg': _r(bridge['activity_effect'][product]),
            'added_effect_kg': _r(bridge['added_effect'][product]),
            'removed_effect_kg': _r(bridge['removed_effect'][product]),
            'relink_effect_kg': _r(bridge['relink_effect'][product]),
            'new_kg': _r(sum(
                _line_value(activity, new_factors[activity['factor']]).get(product, 0.0)
                for activity in new_data['activities']
                if new_factors.get(activity['factor']) is not None
            )),
        })

    # 不确定性口径变化（各自按当时资料计算，不互相覆盖）
    old_uncertainty = analyze_uncertainty(old_data)
    new_uncertainty = analyze_uncertainty(new_data)
    uncertainty_change = {
        'old_total': old_uncertainty['total'],
        'new_total': new_uncertainty['total'],
        'old_unsupported_claims': old_uncertainty['unsupported_claims'],
        'new_unsupported_claims': new_uncertainty['unsupported_claims'],
        'note': '两端不确定性均按各自 revision 的资料独立计算；区间来源不会因换版本获得分布含义',
    }

    missing_now = [activity['id'] for activity in new_data['activities']
                   if new_factors.get(activity['factor']) is None]
    missing_then = [activity['id'] for activity in old_data['activities']
                    if old_factors.get(activity['factor']) is None]

    return {
        'interpretation': {
            'data_revision_effects': ['factor_effect', 'allocation_effect'],
            'operational_effects': ['activity_effect', 'added_effect', 'removed_effect', 'relink_effect'],
            'rule': '资料修订只改变因子与分摊依据；活动量、活动结构与因子挂接变化归为经营活动变化',
            'historical_integrity': '旧版本数字始终来自旧 revision，比较结果不回写、不覆盖历史发布',
        },
        'known_total_kg': {'old': _r(old_summary_known), 'new': _r(new_summary_known),
                           'delta': _r(new_summary_known - old_summary_known)},
        'bridge_total_kg': effect_totals,
        'reconciliation_ok': reconciliation_ok,
        'product_bridge': product_bridge,
        'factor_revisions': factor_changes,
        'activity_changelog': changelog,
        'uncertainty_change': uncertainty_change,
        'missing_factor_activities': {'old': missing_then, 'new': missing_now},
        'meaning': '桥接各项可相加解释总量变化；缺因子活动的排放未知，其变化不进入中心值桥接',
    }
