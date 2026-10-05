from copy import deepcopy
from decimal import Decimal


MANAGEMENT = {'add', 'close_partial', 'close_full', 'set_stop',
              'set_take_profit', 'cancel'}
AMBIGUOUS_LINK_QUESTION = 'Не удалось однозначно связать действие с ранее распознанным входом.'


def _candidate_leverage(candidate):
    low = candidate.get('leverage_min')
    if low is None:
        return None
    high = candidate.get('leverage_max') or low
    return int((Decimal(low) + Decimal(high)) // 2)


def _distance(left, right):
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def _score(signal, candidate):
    if signal.side and candidate['side'] != signal.side:
        return -100, []
    score, reasons = 2, ['совпадает направление']
    if signal.symbol == candidate['symbol']:
        score += 6
        reasons.append('совпадает тикер')
    elif signal.symbol:
        left = signal.symbol.removesuffix('USDT')
        right = candidate['symbol'].removesuffix('USDT')
        if _distance(left, right) <= 1:
            score += 1
            reasons.append('тикеры отличаются на один знак')
    if (signal.reference_entry_price and
            signal.reference_entry_price in candidate.get('entry_prices', [])):
        score += 8
        reasons.append('совпадает цена открытия автора')
    leverage = signal.proposed_leverage()
    candidate_leverage = _candidate_leverage(candidate)
    if leverage is not None and leverage == candidate_leverage:
        score += 3
        reasons.append('совпадает плечо')
    if signal.related_message_id == candidate['source_message_id']:
        score += 20
        reasons.append('совпадает связанный пост')
    return score, reasons


def correlate(analysis, candidates):
    result = deepcopy(analysis)
    notes = []
    for signal in result.signals:
        if signal.action not in MANAGEMENT or not candidates:
            continue
        ranked = sorted(((*_score(signal, candidate), candidate) for candidate in candidates),
                        key=lambda item: item[0], reverse=True)
        best_score, reasons, best = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else -100
        strong_identity = ('совпадает тикер' in reasons or
                           ('совпадает цена открытия автора' in reasons and
                            ('совпадает плечо' in reasons or
                             'тикеры отличаются на один знак' in reasons)))
        if best_score < 8 or best_score - second_score < 3 or not strong_identity:
            if AMBIGUOUS_LINK_QUESTION not in signal.questions:
                signal.questions.append(AMBIGUOUS_LINK_QUESTION)
            continue
        old_symbol = signal.symbol
        signal.symbol = best['symbol']
        signal.side = best['side']
        signal.related_message_id = best['source_message_id']
        signal.questions = [item for item in signal.questions
                            if item != AMBIGUOUS_LINK_QUESTION]
        reason = ', '.join(reasons)
        note = f'Сопоставлено с входом #{best["source_message_id"]}: {reason}.'
        if old_symbol and old_symbol != signal.symbol:
            result.summary = result.summary.replace(old_symbol, signal.symbol)
            signal.evidence = signal.evidence.replace(old_symbol, signal.symbol)
            note += f' Первичное чтение тикера {old_symbol} исправлено на {signal.symbol}.'
        signal.evidence = (signal.evidence.rstrip() + ' ' + note).strip()
        notes.append(note)
    return result, notes
