from datetime import datetime, timezone


def valid_market_check(status='valid', reason='test contract is valid'):
    return {
        'status': status, 'reason': reason, 'mexc_symbol': 'AVA_USDT',
        'contract': {
            'symbol': 'AVA_USDT', 'positionOpenType': 3, 'futureType': 1,
            'contractSize': 1, 'minLeverage': 1, 'maxLeverage': 20,
            'countryConfigContractMaxLeverage': 0, 'priceUnit': 0.0001,
            'volUnit': 1, 'minVol': 1, 'maxVol': 3500, 'state': 0,
            'apiAllowed': True, 'settleCoin': 'USDT', 'quoteCoin': 'USDT',
        },
        'ticker': {'symbol': 'AVA_USDT', 'lastPrice': 0.2839,
                   'bid1': 0.2838, 'ask1': 0.2840},
        'candles': {}, 'current_price': '0.2839', 'bid_price': '0.2838',
        'ask_price': '0.284', 'estimated_contracts': '352',
        'estimated_base_quantity': '352', 'estimated_notional_usdt': '99.8976',
        'observed_high': None, 'observed_low': None, 'signal_age_seconds': 60,
        'checked_at': datetime.now(timezone.utc).isoformat(),
    }
