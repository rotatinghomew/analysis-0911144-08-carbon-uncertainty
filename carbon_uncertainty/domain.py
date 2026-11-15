from collections import defaultdict
from .common import finite, require, table, unique

UNITS = {'kWh': ('energy', 1), 'MWh': ('energy', 1000), 'L': ('volume', 1), 'mL': ('volume', 0.001)}


def convert(amount, source, target):
    require(source in UNITS and target in UNITS, '单位不支持')
    source_kind, source_scale = UNITS[source]
    target_kind, target_scale = UNITS[target]
    require(source_kind == target_kind, '不能跨量纲换算')
    return amount * source_scale / target_scale


def validate(data):
    factors = table(data, 'factors')
    activities = table(data, 'activities')
    unique(factors)
    unique(activities)
    mapping = {row['id']: row for row in factors}
    for factor in factors:
        require(factor['unit'] in UNITS and bool(factor['source']), '因子单位或来源不完整')
        finite(factor['kg_per_unit'], 'kg_per_unit', 0)
        uncertainty = factor['uncertainty']
        require(uncertainty['kind'] in ('interval', 'normal'), '不确定性类型不支持')
        if uncertainty['kind'] == 'interval':
            low = finite(uncertainty['low'], 'low', 0)
            high = finite(uncertainty['high'], 'high', 0)
            require(low <= factor['kg_per_unit'] <= high, '因子不在声明范围内')
        else:
            finite(uncertainty['sd'], 'sd', 0)
    for activity in activities:
        finite(activity['amount'], 'amount', 0)
        require(activity['unit'] in UNITS, '活动单位不支持')
        if activity['factor'] in mapping:
            convert(activity['amount'], activity['unit'], mapping[activity['factor']]['unit'])
        for value in activity['allocation'].values():
            finite(value, 'allocation', 0)
        require(abs(sum(activity['allocation'].values()) - 1) < 1e-8, '分摊不守恒')


def records(data):
    return data['activities']


def factor_mapping(data):
    return {row['id']: row for row in data['factors']}


def emission_line(activity, factor):
    """单条活动的中心值排放（kg），单位变换依赖在此统一保留。"""
    converted_amount = convert(activity['amount'], activity['unit'], factor['unit'])
    return converted_amount * factor['kg_per_unit'], converted_amount


def summarize(data):
    mapping = factor_mapping(data)
    totals = defaultdict(float)
    missing = []
    for activity in data['activities']:
        factor = mapping.get(activity['factor'])
        if factor is None:
            missing.append(activity['id'])
            continue
        value, _ = emission_line(activity, factor)
        for product, share in activity['allocation'].items():
            totals[product] += value * share
    return {'known_kg': sum(totals.values()), 'products_kg': dict(totals), 'missing_factor_activities': missing, 'complete': not missing, 'meaning': '中心值核算，尚未传播不确定性'}
