"""不确定性传播、信息缺口与补测优先级。

依赖模型（与 docs/data-contract.md 对齐）：
- 同一 source 的因子共享一个不确定性驱动：同来源的所有活动行完全相关，
  不会因为因子在几百行出现就被算作几百份独立证据。
- 单位换算与分摊比例是确定性线性变换，只改变权重，不改变或独立化不确定性。
- interval 仅声明上下界、不声明分布：只能做界传播（comonotonic 极值），
  不能生成标准差、分位数或覆盖率承诺。
- normal 才声明标准差；不同 source 之间是否独立未知，独立假设下的数字必须显式标注。
- 缺失因子的活动不进入任何总量，作为信息缺口输出，绝不按零排放处理。
"""
import math
from collections import defaultdict

from .domain import emission_line, factor_mapping

INTERVAL = 'interval'
NORMAL = 'normal'


def _round(value):
    if value is None:
        return None
    return round(float(value), 9)


def _group_activities(data):
    """把活动按因子来源分组，同时收集缺因子活动。"""
    mapping = factor_mapping(data)
    groups = defaultdict(lambda: {'factors': {}, 'kinds': set(), 'rows': []})
    missing = []
    for activity in data['activities']:
        factor = mapping.get(activity['factor'])
        if factor is None:
            missing.append(activity)
            continue
        converted, converted_amount = emission_line(activity, factor)
        group = groups[factor['source']]
        group['factors'][factor['id']] = factor
        group['kinds'].add(factor['uncertainty']['kind'])
        group['rows'].append({
            'activity_id': activity['id'],
            'factor_id': factor['id'],
            'converted_amount': converted_amount,
            'center_kg': converted,
            'allocation': activity['allocation'],
            'raw_amount': activity['amount'],
            'raw_unit': activity['unit'],
        })
    return groups, missing, mapping


def _source_contributions(group):
    """返回每个活动行对来源中心值、下界、上界、标准差的贡献。

    同一来源共享同一驱动，整组在共同极值处取得总界（完全相关，
    而不是把各行误差独立化后相消）；线性权重来自换算后的活动量。
    """
    rows = []
    center = low = high = sd = 0.0
    for row in group['rows']:
        factor = group['factors'][row['factor_id']]
        amount = row['converted_amount']
        f0 = factor['kg_per_unit']
        entry = dict(row)
        center += row['center_kg']
        if factor['uncertainty']['kind'] == INTERVAL:
            low += amount * factor['uncertainty']['low']
            high += amount * factor['uncertainty']['high']
            entry['low_kg'] = amount * factor['uncertainty']['low']
            entry['high_kg'] = amount * factor['uncertainty']['high']
        else:
            row_sd = amount * factor['uncertainty']['sd']
            sd += row_sd
            entry['sd_kg'] = row_sd
        rows.append(entry)
    return rows, center, low, high, sd


def _frechet_sd_bounds(source_sds):
    """来源间相关性未知时，加权和标准差的 Fréchet 可行界。"""
    if not source_sds:
        return 0.0, 0.0
    total = sum(source_sds)
    lower = max(0.0, 2.0 * max(source_sds) - total)
    return lower, total


def analyze_uncertainty(data):
    groups, missing_activities, _ = _group_activities(data)

    products = sorted({product for activity in data['activities'] for product in activity['allocation']})
    product_center = defaultdict(float)
    for group in groups.values():
        for row in group['rows']:
            for product, share in row['allocation'].items():
                product_center[product] += row['center_kg'] * share

    sources_out = []
    # 产品级累计量
    interval_low = defaultdict(float)
    interval_high = defaultdict(float)
    normal_source_vectors = []
    normal_source_sds = []
    interval_sources = []
    has_unbounded = False

    for source in sorted(groups):
        group = groups[source]
        rows, center, low, high, sd = _source_contributions(group)
        kinds = sorted(group['kinds'])
        leverage_total = center if center > 0 else 0.0
        for row in rows:
            row['leverage'] = (row['center_kg'] / leverage_total) if leverage_total > 0 else 0.0
        source_entry = {
            'source': source,
            'factor_ids': sorted(group['factors']),
            'uncertainty_kinds': kinds,
            'shared_driver': True,
            'activity_count': len(rows),
            'center_kg': _round(center),
            'activities': [
                {
                    'activity_id': row['activity_id'],
                    'factor_id': row['factor_id'],
                    'converted_amount_kg_basis': _round(row['converted_amount']),
                    'center_kg': _round(row['center_kg']),
                    'leverage_share': _round(row['leverage']),
                    **({'low_kg': _round(row['low_kg']), 'high_kg': _round(row['high_kg'])}
                       if 'low_kg' in row else {'sd_kg': _round(row['sd_kg'])}),
                }
                for row in rows
            ],
        }
        if kinds == [INTERVAL]:
            source_entry.update({
                'hard_low_kg': _round(low),
                'hard_high_kg': _round(high),
                'half_width_kg': _round((high - low) / 2.0),
                'sd_kg': None,
            })
            interval_sources.append(source)
            for row in rows:
                for product, share in row['allocation'].items():
                    interval_low[product] += row['low_kg'] * share
                    interval_high[product] += row['high_kg'] * share
        elif kinds == [NORMAL]:
            source_entry.update({
                'hard_low_kg': None,
                'hard_high_kg': None,
                'sd_kg': _round(sd),
            })
            has_unbounded = True
            vector = {}
            for row in rows:
                for product, share in row['allocation'].items():
                    vector[product] = vector.get(product, 0.0) + row['sd_kg'] * share
            normal_source_vectors.append(vector)
            normal_source_sds.append(sd)
        else:
            # 同一来源混合 interval/normal：声明的信息互相冲突，不支持合成界或标准差
            source_entry.update({
                'hard_low_kg': None,
                'hard_high_kg': None,
                'sd_kg': None,
                'unsupported': '同一来源混合区间与分布声明，无法合成统一的不确定性量',
            })
            has_unbounded = True
        sources_out.append(source_entry)

    # 产品级结果
    products_out = []
    independent_supported = bool(normal_source_vectors) and not interval_sources and not any(
        s.get('unsupported') for s in sources_out
    )
    for product in products:
        entry = {
            'product': product,
            'center_kg': _round(product_center[product]),
            'hard_low_kg': None,
            'hard_high_kg': None,
            'sd_independent_sources_kg': None,
            'sd_range_unknown_correlation_kg': None,
        }
        if independent_supported:
            variance = sum(vector.get(product, 0.0) ** 2 for vector in normal_source_vectors)
            entry['sd_independent_sources_kg'] = _round(math.sqrt(variance))
            lower, upper = _frechet_sd_bounds([vector.get(product, 0.0) for vector in normal_source_vectors])
            entry['sd_range_unknown_correlation_kg'] = [_round(lower), _round(upper)]
        if not has_unbounded and interval_sources:
            entry['hard_low_kg'] = _round(interval_low[product])
            entry['hard_high_kg'] = _round(interval_high[product])
        products_out.append(entry)

    # 总量（与产品合计一致）
    total_center = sum(product_center.values())
    total_entry = {
        'center_kg': _round(total_center),
        'hard_low_kg': None,
        'hard_high_kg': None,
        'sd_independent_sources_kg': None,
        'sd_range_unknown_correlation_kg': None,
    }
    if independent_supported:
        total_variance = sum(sd ** 2 for sd in normal_source_sds)
        total_entry['sd_independent_sources_kg'] = _round(math.sqrt(total_variance))
        lower, upper = _frechet_sd_bounds(normal_source_sds)
        total_entry['sd_range_unknown_correlation_kg'] = [_round(lower), _round(upper)]
    if not has_unbounded and interval_sources:
        total_entry['hard_low_kg'] = _round(sum(interval_low.values()))
        total_entry['hard_high_kg'] = _round(sum(interval_high.values()))

    # 仅 normal 且来源独立假设成立时，产品协方差矩阵才有解析支持
    covariance = None
    if independent_supported and len(products) > 1:
        matrix = []
        for product_a in products:
            line = []
            for product_b in products:
                value = sum(vector.get(product_a, 0.0) * vector.get(product_b, 0.0)
                            for vector in normal_source_vectors)
                line.append(_round(value))
            matrix.append(line)
        covariance = {'products': products, 'matrix_kg2': matrix,
                      'basis': '仅在不同 source 相互独立的假设下成立；来源间相关性未知时不构成承诺'}

    # 信息缺口与补测优先级
    gaps = []
    for activity in missing_activities:
        gaps.append({
            'kind': 'missing_factor',
            'ref': activity['id'],
            'severity': 'unknown_emission',
            'reason': '活动缺少可用因子，排放完全未知，不能按零计入总量',
            'potential_gain': '补因子后该活动从缺口转为可核算，当前无法量化收益上界',
        })
    interval_ranked = sorted(
        (s for s in sources_out if s['uncertainty_kinds'] == [INTERVAL]),
        key=lambda s: s['half_width_kg'], reverse=True,
    )
    for index, source_entry in enumerate(interval_ranked, start=1):
        top_activity = max(source_entry['activities'], key=lambda row: row['leverage_share'])
        gaps.append({
            'kind': 'interval_only_source',
            'ref': source_entry['source'],
            'severity': 'range_without_distribution',
            'rank': index,
            'half_width_kg': source_entry['half_width_kg'],
            'reason': '供应商仅提供区间，区间宽度直接构成总量硬界宽度，且无法支持分位数或覆盖率',
            'top_activity_id': top_activity['activity_id'],
            'top_activity_leverage_share': top_activity['leverage_share'],
            'potential_gain': '在该来源补测分布或收窄区间，按来源半宽与活动杠杆比例缩小总量界宽',
        })
    normal_ranked = sorted(
        (s for s in sources_out if s['uncertainty_kinds'] == [NORMAL]),
        key=lambda s: s['sd_kg'] ** 2, reverse=True,
    )
    for index, source_entry in enumerate(normal_ranked, start=1):
        top_activity = max(source_entry['activities'], key=lambda row: row['leverage_share'])
        gaps.append({
            'kind': 'normal_source_variance',
            'ref': source_entry['source'],
            'severity': 'variance_contribution',
            'rank': index,
            'variance_kg2': _round(source_entry['sd_kg'] ** 2),
            'top_activity_id': top_activity['activity_id'],
            'top_activity_leverage_share': top_activity['leverage_share'],
            'potential_gain': '降低该来源 sd 按其方差占比缩小独立假设下的总量标准差',
        })

    unsupported_claims = []
    if interval_sources:
        unsupported_claims.append(
            '存在仅声明区间的来源：不支持概率分位数、标准差或任何覆盖率（如 95%）承诺；硬界之外的概率表述无依据'
        )
    if normal_source_vectors and interval_sources:
        unsupported_claims.append(
            '区间来源与分布来源混合：总量不存在有限硬界，也不支持整体覆盖率承诺；分布类数字仅限 normal 来源小计'
        )
    if any(s.get('unsupported') for s in sources_out):
        unsupported_claims.append(
            '存在同来源混合声明：该来源的界与标准差均无法合成，相关风险数字不被支持'
        )
    if normal_source_vectors:
        unsupported_claims.append(
            '不同 source 间相关性未知：独立来源假设下的 sd 与协方差仅为条件结果，相关性上升时实际 sd 可达到 sd_range 上界'
        )
    if not groups:
        unsupported_claims.append('没有任何带因子的活动，无法形成不确定性结论')

    return {
        'revision_scope': '调用方须与 revision、digest 一起保存；本结果只解释该资料版本',
        'total': total_entry,
        'products': products_out,
        'covariance': covariance,
        'sources': sources_out,
        'information_gaps': gaps,
        'unsupported_claims': unsupported_claims,
        'assumptions': [
            '活动量与分摊比例按确定性输入处理，其本身未声明不确定性，故不参与传播',
            '同一 source 内因子共享驱动、完全相关；单位换算与分摊仅做线性权重变换',
            '缺因子活动不计入 known 总量，其排放未知而非为零',
        ],
        'meaning': '区间结果为硬界（不含概率承诺）；sd 类结果仅在标注的独立来源假设下成立',
    }
