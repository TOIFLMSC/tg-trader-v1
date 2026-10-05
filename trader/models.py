from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# Expose lexical constraints to the model's strict tool schema, not just Python.
# Units belong in the displayed card; numeric fields must never contain "20x"/"75%".
PositiveNumber = Annotated[str, Field(
    pattern=r'^(?:0\.[0-9]*[1-9][0-9]*|[1-9][0-9]*(?:\.[0-9]+)?)$',
    description='Positive decimal string using a dot, without units, spaces, x or %. Example: "20", not "20x".')]
Ticker = Annotated[str, Field(pattern=r'^[A-Z0-9]{1,35}$')]


class Signal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['open', 'add', 'close_partial', 'close_full', 'set_stop',
                    'set_take_profit', 'cancel', 'scenario', 'report', 'unknown']
    symbol: Ticker | None
    side: Literal['long', 'short'] | None
    entry_kind: Literal['market', 'trigger_limit', 'unspecified']
    entry_prices: list[PositiveNumber]
    leverage_min: PositiveNumber | None
    leverage_max: PositiveNumber | None
    stop_price: PositiveNumber | None
    take_profits: list[PositiveNumber]
    close_percent: PositiveNumber | None
    reference_entry_price: PositiveNumber | None
    related_message_id: int | None
    evidence: str
    questions: list[str]

    @field_validator('symbol')
    @classmethod
    def symbol_valid(cls, value):
        if value is not None and (not value.isascii() or not value.isalnum()
                                  or value != value.upper() or len(value) > 35):
            raise ValueError('Expected uppercase ticker, no separators')
        return value

    @model_validator(mode='after')
    def numbers_valid(self):
        values = self.entry_prices + self.take_profits + [self.leverage_min,
                 self.leverage_max, self.stop_price, self.close_percent,
                 self.reference_entry_price]
        for value in values:
            if value is None:
                continue
            try:
                number = Decimal(value)
            except InvalidOperation:
                raise ValueError('Invalid decimal') from None
            if not number.is_finite() or number <= 0:
                raise ValueError('Numbers must be finite and positive')
        if self.close_percent and Decimal(self.close_percent) > 100:
            raise ValueError('Close percent exceeds 100')
        if self.leverage_max and not self.leverage_min:
            raise ValueError('Missing leverage minimum')
        if self.leverage_min and self.leverage_max and Decimal(self.leverage_min) > Decimal(self.leverage_max):
            raise ValueError('Reversed leverage range')
        return self

    def proposed_leverage(self):
        if self.leverage_min is None:
            return None
        low = Decimal(self.leverage_min)
        high = Decimal(self.leverage_max or self.leverage_min)
        return int(((low + high) / 2).to_integral_value(rounding=ROUND_FLOOR))


class Analysis(BaseModel):
    model_config = ConfigDict(extra='forbid')
    summary: str
    signals: list[Signal]


LABELS = {'open': 'Вход', 'add': 'Добавление к позиции', 'close_partial': 'Частичное закрытие',
          'close_full': 'Полное закрытие', 'set_stop': 'Изменение стопа',
          'set_take_profit': 'Тейк-профит', 'cancel': 'Отмена',
          'scenario': 'Сценарий / ожидание', 'report': 'Отчёт автора', 'unknown': 'Неясное действие'}


def render(analysis, channel, message_id, reply_id, edited=False, old=False):
    lines = ['РАЗБОР СИГНАЛА',
             f'Канал: {channel}', f'Пост: #{message_id}' + (' · изменён' if edited else ''),
             f'Ответ на: #{reply_id}' if reply_id else 'Без реплая', '', analysis.summary[:600]]
    if old:
        lines.append('Коллу больше 10 минут: актуальность сценария перед входом ещё не проверена.')
    for signal in analysis.signals[:8]:
        lines.extend(['', f'{LABELS[signal.action]} · {signal.symbol or "тикер не определён"} · '
                      f'{(signal.side or "направление не определено").upper()}'])
        if signal.action in ('open', 'add', 'scenario'):
            leverage = signal.proposed_leverage()
            lines.append(f'Плечо: {leverage}×' if leverage else 'Плечо не указано: потребуется выбор, предложим 1×.')
            lines.append('Тип входа: ' + {'market': 'рыночный', 'trigger_limit': 'trigger-limit',
                                         'unspecified': 'требует уточнения'}[signal.entry_kind])
            lines.append('Уровни входа автора: ' + (', '.join(signal.entry_prices) or 'не указаны'))
        if signal.close_percent:
            lines.append(f'Закрытие: {signal.close_percent}% оставшегося объёма')
        elif signal.action == 'close_partial':
            lines.append('Доля закрытия не указана: потребуется уточнение.')
        if signal.stop_price:
            lines.append(f'Стоп автора: {signal.stop_price}')
        elif signal.action in ('open', 'add', 'scenario'):
            lines.append('Стоп не указан; свой не добавляем.')
        if signal.take_profits:
            lines.append('Тейки: ' + ', '.join(signal.take_profits))
        if signal.reference_entry_price and signal.action not in ('open', 'add'):
            lines.append(f'Цена исходного входа автора: {signal.reference_entry_price}')
        if signal.related_message_id:
            lines.append(f'Связь с исходным постом: #{signal.related_message_id}')
        lines.append('Основание: ' + signal.evidence[:400])
        lines.extend('Уточнить: ' + q[:250] for q in signal.questions[:5])
    lines.extend([
        '',
        'Распознавание завершено. Фактическое состояние и исполнение показаны в карточке плана.',
    ])
    return '\n'.join(lines)
