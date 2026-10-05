"""Local connection setup only. No trading and no channel content sent to an LLM."""
import argparse
import asyncio
from getpass import getpass
import logging
from pathlib import Path
import secrets
import ssl
import sys
import time

from dotenv import dotenv_values, set_key
import httpx

ROOT = Path(__file__).resolve().parent
ENV = ROOT / '.env'
REQUIRED = ('TELEGRAM_API_ID', 'TELEGRAM_API_HASH', 'TELEGRAM_PHONE',
            'TELEGRAM_BOT_TOKEN', 'OPENAI_API_KEY', 'OPENAI_MODEL')
logging.disable(logging.CRITICAL)  # Never log request URLs containing bot tokens.


class SetupError(Exception):
    pass


def settings():
    return dotenv_values(ENV, encoding='utf-8-sig', interpolate=False)


def require(cfg, *keys):
    missing = [key for key in keys if not cfg.get(key)]
    if missing:
        raise SetupError('Missing settings: ' + ', '.join(missing))


def local_check(cfg):
    for key in REQUIRED:
        print(f'{key}: ' + ('SET' if cfg.get(key) else 'MISSING'))
    require(cfg, *REQUIRED)
    if not cfg['TELEGRAM_API_ID'].isdigit():
        raise SetupError('TELEGRAM_API_ID must be numeric.')
    phone = cfg['TELEGRAM_PHONE']
    if not phone.startswith('+') or not phone[1:].isdigit():
        raise SetupError('TELEGRAM_PHONE must use international format: + and digits.')
    print('Local configuration OK. Secret values not displayed.')


async def bot_call(client, token, method, payload=None):
    response = await client.post(f'https://api.telegram.org/bot{token}/{method}',
                                 json=payload or {})
    if response.status_code != 200:
        raise SetupError(f'Bot API {method}: HTTP {response.status_code}.')
    result = response.json()
    if not result.get('ok'):
        raise SetupError(f'Bot API {method}: request rejected.')
    return result['result']


async def network_check(cfg, llm=False):
    require(cfg, 'TELEGRAM_BOT_TOKEN')
    async with httpx.AsyncClient(timeout=45, follow_redirects=False, verify=ssl.create_default_context()) as client:
        bot = await bot_call(client, cfg['TELEGRAM_BOT_TOKEN'], 'getMe')
        print(f'Bot API OK: @{bot["username"]}')
        if llm:
            require(cfg, 'OPENAI_API_KEY', 'OPENAI_MODEL')
            response = await client.post('https://api.openai.com/v1/responses',
                headers={'Authorization': 'Bearer ' + cfg['OPENAI_API_KEY']},
                json={'model': cfg['OPENAI_MODEL'], 'input': 'Reply with OK.',
                      'max_output_tokens': 32, 'reasoning': {'effort': 'none'},
                      'store': False})
            if response.status_code != 200:
                raise SetupError(f'OpenAI: HTTP {response.status_code}. Check key, model access and billing.')
            result = response.json()
            output = ''.join(part.get('text', '') for item in result.get('output', [])
                             for part in item.get('content', []) if part.get('type') == 'output_text')
            if not output.strip():
                raise SetupError('OpenAI returned no text. Check model configuration.')
            usage = result.get('usage', {})
            print(f'LLM response received. Status: {result.get("status")}; '
                  f'tokens: {usage.get("total_tokens", "unknown")}.')


async def telegram_setup(cfg):
    from telethon import TelegramClient
    from telethon.utils import get_peer_id
    require(cfg, 'TELEGRAM_API_ID', 'TELEGRAM_API_HASH', 'TELEGRAM_PHONE')
    session = (ROOT / cfg.get('TELEGRAM_SESSION_PATH', 'data/telegram/reader.session')).resolve()
    if not session.is_relative_to(ROOT / 'data'):
        raise SetupError('Session path must be inside project data/.')
    session.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(session), int(cfg['TELEGRAM_API_ID']), cfg['TELEGRAM_API_HASH'])
    try:
        await client.start(phone=cfg['TELEGRAM_PHONE'],
                           code_callback=lambda: getpass('Telegram login code (hidden): '),
                           password=lambda: getpass('Telegram 2FA password (hidden): '))
        me = await client.get_me()
        if not me or me.bot or me.phone != cfg['TELEGRAM_PHONE'].lstrip('+'):
            raise SetupError('Session account does not match TELEGRAM_PHONE. No channels selected.')
        print('Dedicated Telegram account authorized.')
        channels = [d async for d in client.iter_dialogs() if getattr(d.entity, 'broadcast', False)]
        if not channels:
            raise SetupError('No subscribed channels found. Join the initial channel in Telegram first.')
        for number, dialog in enumerate(channels, 1):
            print(f'{number}. {dialog.name}')
        selection = int(input('Select TEST channel number: '))
        if not 1 <= selection <= len(channels):
            raise SetupError('Invalid selection.')
        selected = channels[selection - 1]
        if input(f'Use channel "{selected.name}"? Type YES: ').strip() != 'YES':
            print('Selection cancelled.')
            return
        set_key(ENV, 'TELEGRAM_TEST_CHANNEL_ID', str(get_peer_id(selected.entity)))
        print('Channel ID saved in .env. No messages downloaded.')
    finally:
        await client.disconnect()


def owner_candidate(update, command):
    msg = update.get('message', {})
    sender = msg.get('from', {})
    chat = msg.get('chat', {})
    if (msg.get('text', '').strip() == command and chat.get('type') == 'private'
            and sender.get('id') == chat.get('id') and not sender.get('is_bot', True)):
        return sender
    return None


async def bind_owner(cfg):
    require(cfg, 'TELEGRAM_BOT_TOKEN')
    token = cfg['TELEGRAM_BOT_TOKEN']
    command = '/start ' + secrets.token_urlsafe(24)
    async with httpx.AsyncClient(timeout=35, follow_redirects=False, verify=ssl.create_default_context()) as client:
        info = await bot_call(client, token, 'getWebhookInfo')
        if info.get('url'):
            raise SetupError('Bot has an active webhook. Use a dedicated test bot; webhook was not changed.')
        bot = await bot_call(client, token, 'getMe')
        print(f'From your PERSONAL account send this exact command to @{bot["username"]}:')
        print(command)
        print('Waiting up to 5 minutes. Keep this local code private.')
        deadline, offset = time.monotonic() + 300, 0
        while time.monotonic() < deadline:
            updates = await bot_call(client, token, 'getUpdates',
                                     {'offset': offset, 'timeout': 20, 'allowed_updates': ['message']})
            for update in updates:
                offset = update['update_id'] + 1
                sender = owner_candidate(update, command)
                if not sender:
                    continue
                print(f'Candidate: id={sender["id"]}, username=@{sender.get("username", "none")}')
                if input('Confirm this is YOUR personal account. Type YES: ').strip() != 'YES':
                    print('Binding cancelled. Owner unchanged.')
                    return
                set_key(ENV, 'TELEGRAM_OWNER_ID', str(sender['id']))
                print('Owner saved in .env. No trading actions available.')
                return
        raise SetupError('Owner binding timed out. Run again to generate a new code.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check', 'network', 'telegram', 'owner'])
    parser.add_argument('--llm', action='store_true', help='Send one short paid API test request.')
    args = parser.parse_args()
    try:
        cfg = settings()
        if args.command == 'check':
            local_check(cfg)
        elif args.command == 'network':
            asyncio.run(network_check(cfg, args.llm))
        elif args.command == 'telegram':
            asyncio.run(telegram_setup(cfg))
        else:
            asyncio.run(bind_owner(cfg))
        return 0
    except SetupError as error:
        print(f'ERROR: {error}')
    except (KeyboardInterrupt, EOFError):
        print('Cancelled. You can run setup again.')
    except httpx.ConnectError as error:
        causes = []
        current = error
        while current:
            causes.append(str(current).lower())
            current = current.__cause__
        detail = ' '.join(causes)
        category = next((word for word in ('certificate', 'getaddrinfo', 'refused', 'reset', 'proxy')
                         if word in detail), 'unclassified connection failure')
        print(f'ERROR: Connection failed ({category}). Credentials were not displayed.')
    except Exception as error:
        # Exception messages from SDKs can contain credentials, URLs or phone numbers.
        print(f'ERROR: {type(error).__name__}. Details hidden to protect credentials.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
