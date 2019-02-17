#!/usr/bin/python3

import threading
import time
import re
import requests
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
from telegram.ext import (Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler,
                          PicklePersistence)
from telegram.error import TelegramError

from loki_sn_bot_config import TELEGRAM_TOKEN, PERSISTENCE_FILENAME, NODE_URL, OWNER, EXTRA



# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

logger = logging.getLogger(__name__)

pp = None
updater = None

network_info = None
sn_states = None


PROOF_AGE_WARNING = 3600 + 300  # 1 hour plus 5 minutes of grace time (uptime proofs can take slightly more than an hour)
PROOF_AGE_REPEAT = 600  # How often to repeat the alert
STAKE_BLOCKS = 720*30 + 20

def uptime(ts):
    if not ts:
        return '_No proof received_'
    ago = int(time.time() - ts)
    warning = ' ⚠️' if ago >= PROOF_AGE_WARNING else ''
    seconds = ago % 60
    ago //= 60
    minutes = ago % 60
    ago //= 60
    hours = ago
    return ('{}h{:02d}m{:02d}s'.format(hours, minutes, seconds) if hours else
            '{}m{:02d}s'.format(minutes, seconds) if minutes else
            '{}s'.format(seconds)) + ' ago' + warning

def friendly_time(seconds):
    val = ''
    if seconds >= 86400:
        days = seconds // 86400
        val = '{} day{} '.format(days, '' if days == 1 else 's')
        seconds %= 86400
    if seconds >= 3600:
        val += '{:.1f} hours'.format(seconds / 3600)
    elif seconds >= 60:
        val += '{:.0f} minutes'.format(seconds / 60)
    else:
        val += '{} seconds'.format(seconds)
    return val


def alias(sn):
    return sn['alias'] if 'alias' in sn else sn['pubkey'][0:5] + '...' + sn['pubkey'][-3:]


def ago(seconds):
    return friendly_time(seconds) + ' ago'


def moon_symbol(pct):
    return '🌑' if pct < 26 else '🌒' if pct < 50 else '🌓' if pct < 75 else '🌔' if pct < 100 else '🌕'


def escape_markdown(text):
    return text.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")


shutup = set()
def send_reply(bot, update, message, reply_markup=None):
    chat_id = None
    if update.message:
        chat_id = update.message.chat.id
        update.message.reply_markdown(message,
                reply_markup=reply_markup,
                disable_web_page_preview=True)
    else:
        chat_id = update.callback_query.message.chat_id
        bot.send_message(
            chat_id=chat_id,
            text=message, parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup,
            disable_web_page_preview=True)
    if chat_id and chat_id in shutup:
        shutup.remove(chat_id)
        print("removing {} from the shutup list (they contacted me again)", flush=True)


# Send a message to t
def send_message_or_shutup(bot, chatid, message, parse_mode=ParseMode.MARKDOWN, reply_markup=None):
    """Send a message to the bot.  If the message gives a 'bot was blocked by the user' error then we ignore until a send_reply or a restart"""
    if chatid in shutup:
        return False
    try:
        bot.send_message(chatid, message, parse_mode=parse_mode, reply_markup=reply_markup)
    except TelegramError as e:
        if 'bot was blocked by the user' in e.message:
            shutup.add(chatid)
            print("user {} blocked me; stopped sending to them ({})".format(chatid, e), flush=True)
        else:
            print("Error sending message to {}: {}".format(chatid, e), flush=True)
        return False

    return True


def main_menu(bot, update, user_data, reply=''):
    choices = InlineKeyboardMarkup([
        [InlineKeyboardButton('Service node(s)', callback_data='sns'), InlineKeyboardButton('Wallet(s)', callback_data='wallets')],
        [InlineKeyboardButton('Status', callback_data='status')]
    ])

    need_flush = False
    for x in ('want_alias', 'want_note', 'want_wallet'):
        if x in user_data:
            del user_data[x]
            need_flush = True
    if need_flush:
        pp.flush()

    if reply:
        reply += '\n\n'

    if user_data and 'sn' in user_data and user_data['sn']:
        num = len(user_data['sn'])
        reply += "I am currently monitoring *{}* service node{} for you.".format(num, 's' if num != 1 else '')
    else:
        reply += "I am not currently monitoring any service nodes for you."

    send_reply(bot, update, reply, reply_markup=choices)


welcome_message = (
        'Hi!  I can give you loki service node information and send you alerts if the uptime proof for your service node(s) gets too long.  ' +
        'I can also optionally let you know when your service nodes earn a payment and when your service node is nearing expiry.\n' +
        ('\nThis bot is operated by ' + OWNER + '\n' if OWNER else '') +
        ('\n' + EXTRA + '\n' if EXTRA else '')
)


def start(bot, update, user_data):
    if 'sn' not in user_data:
        user_data['sn'] = []
        pp.flush()
    return main_menu(bot, update, user_data, welcome_message)


def intro(bot, update, user_data):
    return main_menu(bot, update, user_data, welcome_message)


def status(bot, update, user_data):
    sns = sn_states
    active, waiting, old_proof = 0, 0, 0
    now = int(time.time())
    for sn in sns.values():
        if sn['total_contributed'] < sn['staking_requirement']:
            waiting += 1
        else:
            active += 1
        if sn['last_uptime_proof'] and now - sn['last_uptime_proof'] > PROOF_AGE_WARNING:
            old_proof += 1

    h = network_info['height']
    reply_text = 'Network height: *{}*\n'.format(network_info['height']);
    reply_text += 'Service nodes: *{}* _(active)_ + *{}* _(awaiting contribution)_\n'.format(active, waiting)
    reply_text += '*{}* service node'.format(old_proof) + (' has uptime proof' if old_proof == 1 else 's have uptime proofs') + ' > 1h5m\n';

    lsr = max(10000 + 35000 * 2**((101250-h)/129600.), min(5*h/2592 + 8000, 15000))
    snbr = 0.5 * (28 + 100 * 2**(-h/64800))
    reply_text += 'Current SN stake requirement: *{:.2f}* LOKI\n'.format(lsr)
    reply_text += 'Current SN reward: *{:.4f}* LOKI'.format(snbr)

    return main_menu(bot, update, user_data, reply_text)


def service_nodes_menu(bot, update, user_data, reply_text=''):
    global sn_states
    sns = []
    for i in range(len(user_data['sn'])):
        sn = user_data['sn'][i]
        status_icon = '🛑'
        if sn['pubkey'] in sn_states:
            info = sn_states[sn['pubkey']]

            proof_age = int(time.time() - info['last_uptime_proof'])
            if proof_age >= PROOF_AGE_WARNING:
                status_icon = '⚠'
            elif info['total_contributed'] < info['staking_requirement']:
                status_icon = moon_symbol(info['total_contributed'] / info['staking_requirement'] * 100)
            elif info['registration_height'] + STAKE_BLOCKS - network_info['height'] < 48*30:
                status_icon = '⏱'
            else:
                status_icon = '💚'
        shortpub = sn['pubkey'][0:5] + '…' + sn['pubkey'][-2:]
        snbutton = InlineKeyboardButton(
                status_icon + ' ' + ('{} ({})'.format(sn['alias'], shortpub) if 'alias' in sn else shortpub),
                callback_data='sn:{}'.format(i))
        if i % 2 == 0:
            sns.append([snbutton])
        else:
            sns[-1].append(snbutton)


    sns.append([InlineKeyboardButton('Add a service node', callback_data='add_sn'),
        InlineKeyboardButton('Show expirations', callback_data='sns_expiries')]);
    sns.append([
        InlineKeyboardButton('<< Main menu', callback_data='main')])

    sn_menu = InlineKeyboardMarkup(sns)
    if reply_text:
        reply_text += '\n\n'
    reply_text += 'View an existing service node, or add a new one?'

    send_reply(bot, update, reply_text, reply_markup=sn_menu)


def service_nodes_expiries(bot, update, user_data):
    global sn_states, network_info
    sns = []
    for i in range(len(user_data['sn'])):
        sn = user_data['sn'][i]
        row = { 'sn': sn }
        row['icon'] = '🛑'
        if sn['pubkey'] in sn_states:
            info = sn_states[sn['pubkey']]

            proof_age = int(time.time() - info['last_uptime_proof'])
            if proof_age >= PROOF_AGE_WARNING:
                row['icon'] = '⚠'
            elif info['total_contributed'] < info['staking_requirement']:
                row['icon'] = moon_symbol(info['total_contributed'] / info['staking_requirement'] * 100)
            elif info['registration_height'] + STAKE_BLOCKS - network_info['height'] < 48*30:
                row['icon'] = '⏱'
            else:
                row['icon'] = '💚'
            row['info'] = info

        sns.append(row)

    sns.sort(key=lambda s: s['info']['registration_height'] if 'info' in s else 0)

    msg = '*Service nodes expirations:*\n'
    for sn in sns:
        msg += '{} {}: '.format(sn['icon'], alias(sn['sn']))
        if 'info' in sn:
            expiry_block = sn['info']['registration_height'] + STAKE_BLOCKS
            msg += 'Block _{}_ (_{}_)\n'.format(
                    expiry_block, friendly_time(120 * (expiry_block + 1 - network_info['height'])))
        else:
            msg += 'Expired/deregistered\n'

    service_nodes_menu(bot, update, user_data, reply_text=msg)


def service_node_add(bot, update, user_data):
    send_reply(bot, update, 'Okay, send me the public key of the service node to add', None)


def service_node_menu(bot, update, user_data):
    i = int(update.callback_query.data.split(':', 1)[1])
    return service_node(bot, update, user_data, i)


def service_node_menu_inplace(bot, update, user_data):
    i = int(update.callback_query.data.split(':', 1)[1])
    return service_node(bot, update, user_data, i,
            callback=lambda msg, reply_markup: bot.edit_message_text(
                text=msg, parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup, chat_id=update.callback_query.message.chat_id,
                message_id=update.callback_query.message.message_id))


def service_node_input(bot, update, user_data):

    if 'want_note' in user_data:
        i = user_data['want_note']
        sn = user_data['sn'][i]
        sn['note'] = update.message.text
        del user_data['want_note']
        pp.flush()
        return service_node(bot, update, user_data, i, 'Updated note for _{}_.  Current status:'.format(alias(sn)))

    elif 'want_alias' in user_data:
        i = user_data['want_alias']
        sn = user_data['sn'][i]
        sn['alias'] = update.message.text.replace("*", "").replace("_", "").replace("[", "").replace("`", "")
        del user_data['want_alias']
        pp.flush()
        return service_node(bot, update, user_data, i, 'Okay, I\'ll now refer to service node _{}_ as _{}_.  Current status:'.format(
            sn['pubkey'], sn['alias']))

    elif 'want_wallet' in user_data:
        wallet = update.message.text
        if not re.match('^L[4-9A-E][1-9A-HJ-NP-Za-km-z]{5,93}$', wallet):
            send_reply(bot, update, 'That doesn\'t look like a valid primary wallet address.  Send me at least the first 7 characters of your primary wallet address')
            return
        del user_data['want_wallet']
        if not 'wallets' in user_data:
            user_data['wallets'] = set()
        user_data['wallets'].add(wallet)
        pp.flush()
        return wallets_menu(bot, update, user_data, 'Added wallet _{}_.  I\'ll now calculate your share of shared contribution service node rewards.'.format(wallet))


    pubkey = update.message.text

    if not re.match('^[0-9a-f]{64}$', pubkey):
        send_reply(bot, update, 'Sorry, I didn\'t understand your message.  Send me a service node public key or use /start')
        return

    if 'sn' not in user_data:
        user_data['sn'] = []
        pp.flush()

    found, found_at = None, None
    for i in range(len(user_data['sn'])):
        if user_data['sn'][i]['pubkey'] == pubkey:
            found = user_data['sn'][i]
            found_at = i

    if found:
        reply_text = 'I am _already_ monitoring service node _{}_ for you.  Current status:'.format(pubkey)

    else:
        found_at = len(user_data['sn'])
        sndata = { 'pubkey': pubkey }
        if pubkey in sn_states:
            sndata['lrbh'] = sn_states[pubkey]['last_reward_block_height']
            if sn_states[pubkey]['total_contributed'] >= sn_states[pubkey]['staking_requirement']:
                sndata['complete'] = True
            reply_text = 'Okay, I\'m now monitoring service node _{}_ for you.  Current status:'.format(pubkey)
        else:
            reply_text = 'Service node _{}_ isn\'t currently registered on the network, but I\'ll start monitoring it for you once it appears.'.format(pubkey)

        user_data['sn'].append(sndata)
        pp.flush()
        found = user_data['sn'][found_at]

    return service_node(bot, update, user_data, found_at, reply_text)


def service_node(bot, update, user_data, i, reply_text = '', callback = None):
    sn = user_data['sn'][i]
    pubkey = sn['pubkey']
    if reply_text:
        reply_text += '\n\n'
    else:
        reply_text = 'Current status of service node _{}_:\n\n'.format(alias(sn))

    reward_notify = 'rewards' in sn and sn['rewards']
    expiry_notifications = 'expires_soon' in sn and sn['expires_soon']
    note = sn['note'] if 'note' in sn else None
    if note:
        reply_text += 'Note: ' + escape_markdown(note) + '\n'

    if pubkey not in sn_states:
        if 'alias' in sn:
            reply_text += 'Service node _{}_ is not registered\n'.format(pubkey)
        else:
            reply_text += 'Not registered\n'
    else:
        info = sn_states[pubkey]
        height = network_info['height']

        reply_text += 'Public key: _{}_\n'.format(pubkey)

        reply_text += 'Last uptime proof: ' + uptime(info['last_uptime_proof']) + '\n'

        expiry_block = info['registration_height'] + STAKE_BLOCKS
        reg_expiry = 'Block *{}* (approx. {})\n'.format(expiry_block, friendly_time(120 * (expiry_block + 1 - height)));

        my_stakes = []
        if 'wallets' in user_data and user_data['wallets']:
            for y in info['contributors']:
                if any(y['address'].startswith(x) for x in user_data['wallets']):
                    my_stakes.append((y['amount'], y['address']))
        if len(my_stakes) == 1:
            my_stakes = 'My stake: *{:.9f}* (_{:.2f}%_)\n'.format(
                    my_stakes[0][0]*1e-9, my_stakes[0][0]/info['staking_requirement']*100)
        elif my_stakes:
            my_stakes = ''.join(
                    'My stake: *{:9f}* (_{:.2f}%_) — _{}...{}_\n'.format(
                        x[0]*1e-9, x[0]/info['staking_requirement']*100, x[1][0:7], x[1][-3:])
                    for x in my_stakes)
        else:
            my_stakes = ''

        if info['total_contributed'] < info['staking_requirement']:
            reply_text += 'Status: *awaiting contribution*\n'
            reply_text += 'Stake: _{:.9f}_ (_{:.1f}%_ of required _{:.9f}_; additional contribution required: {:.9f})\n'.format(
                    info['total_contributed']*1e-9, info['total_contributed'] / info['staking_requirement'] * 100, info['staking_requirement']*1e-9,
                    (info['staking_requirement'] - info['total_contributed'])*1e-9)
            reply_text += my_stakes
            reply_text += 'Registration expiry: ' + reg_expiry
        else:
            reply_text += 'Status: *active*\nStake: *{:.9f}*\n'.format(info['staking_requirement']*1e-9)
            reply_text += my_stakes
            reply_text += 'Registration expiry: ' + reg_expiry
            if info['last_reward_block_height'] > info['registration_height']:
                reply_text += 'Last reward at height *{}* (approx. {})\n'.format(info['last_reward_block_height'], ago(
                    120 * (height - info['last_reward_block_height'])))
            else:
                reply_text += 'Last reward: *never*.\n'

            blocks_to_go = 1
            for sni in list(sn_states.values()):
                if sni['total_contributed'] >= sni['staking_requirement'] and sni['last_reward_block_height'] < info['last_reward_block_height']:
                    blocks_to_go += 1
            reply_text += 'Next reward in *{}* blocks (approx. {})\n'.format(blocks_to_go, friendly_time(blocks_to_go * 120))

        reply_text += 'Reward notifications: *' + ('en' if reward_notify else 'dis') + 'abled*\n'
        reply_text += 'Close-to-expiry notifications: *' + ('en' if expiry_notifications else 'dis') + 'abled*\n'

    menu = InlineKeyboardMarkup([
        [InlineKeyboardButton('Refresh', callback_data='refresh:{}'.format(i)),
         InlineKeyboardButton('View on lokiblocks.com', url='https://lokiblocks.com/service_node/{}'.format(pubkey))],
        [InlineKeyboardButton('Stop monitoring', callback_data='stop:{}'.format(i))],
        [InlineKeyboardButton('Update alias', callback_data='alias:{}'.format(i)),
            InlineKeyboardButton('Remove alias', callback_data='del_alias:{}'.format(i))]
            if 'alias' in sn else
        [InlineKeyboardButton('Set alias', callback_data='alias:{}'.format(i))],
        [InlineKeyboardButton('Change note', callback_data='note:{}'.format(i)),
         InlineKeyboardButton('Delete note', callback_data='del_note:{}'.format(i))]
            if note else
        [InlineKeyboardButton('Add custom note', callback_data='note:{}'.format(i))],
        [InlineKeyboardButton(('Disable' if reward_notify else 'Enable') + ' reward notifications',
            callback_data=('dis' if reward_notify else 'en') + 'able_reward:{}'.format(i))],
        [InlineKeyboardButton(('Disable' if expiry_notifications else 'Enable') + ' close-to-expiry notifications',
            callback_data=('dis' if expiry_notifications else 'en') + 'able_expires_soon:{}'.format(i))],
        [InlineKeyboardButton('< Service nodes', callback_data='sns'), InlineKeyboardButton('<< Main menu', callback_data='main')]
        ])

    if not callback:
        callback = lambda msg, markup: send_reply(bot, update, msg, markup)
    callback(reply_text, menu)


def stop_monitoring(bot, update, user_data):
    i = int(update.callback_query.data.split(':', 1)[1])
    pubkey = user_data['sn'][i]['pubkey']
    del user_data['sn'][i]
    pp.flush()
    return service_nodes_menu(bot, update, user_data, 'Okay, I\'m not longer monitoring service node _{}_ for you.'.format(pubkey))


def add_note(bot, update, user_data):
    i = int(update.callback_query.data.split(':', 1)[1])
    sn = user_data['sn'][i]
    pubkey = sn['pubkey']
    user_data['want_note'] = i
    pp.flush()
    msg = "Send me a custom note to set for service node _{}_.".format(pubkey)
    if 'note' in sn and sn['note']:
        msg += '\n\nThe current note is: ' + escape_markdown(sn['note'])
    send_reply(bot, update, msg)


def del_note(bot, update, user_data):
    i = int(update.callback_query.data.split(':', 1)[1])
    pubkey = user_data['sn'][i]['pubkey']
    del user_data['sn'][i]['note']
    pp.flush()
    return service_node(bot, update, user_data, i, 'Removed note for service node _{}_.'.format(pubkey))


def add_alias(bot, update, user_data):
    i = int(update.callback_query.data.split(':', 1)[1])
    sn = user_data['sn'][i]
    pubkey = sn['pubkey']
    user_data['want_alias'] = i
    pp.flush()
    msg = "Send me an alias to use for this service node instead of the public key (_{}_).".format(pubkey)
    if 'alias' in sn and sn['alias']:
        msg += '\n\nThe current alias is: ' + sn['alias']
    send_reply(bot, update, msg)


def del_alias(bot, update, user_data):
    i = int(update.callback_query.data.split(':', 1)[1])
    pubkey = user_data['sn'][i]['pubkey']
    del user_data['sn'][i]['alias']
    pp.flush()
    return service_node(bot, update, user_data, i, 'Removed alias for service node _{}_.'.format(pubkey))


def disable_reward_notify(bot, update, user_data):
    i = int(update.callback_query.data.split(':', 1)[1])
    pubkey = user_data['sn'][i]['pubkey']
    user_data['sn'][i]['rewards'] = False
    pp.flush()
    return service_node(bot, update, user_data, i, 'Okay, I\'ll no longer send you block reward notifications for _{}_.'.format(pubkey))


def enable_reward_notify(bot, update, user_data):
    i = int(update.callback_query.data.split(':', 1)[1])
    pubkey = user_data['sn'][i]['pubkey']
    user_data['sn'][i]['rewards'] = True
    pp.flush()
    return service_node(bot, update, user_data, i, 'Okay, I\'ll start sending you block reward notifications for _{}_.'.format(pubkey))


def enable_expires_soon(bot, update, user_data):
    i = int(update.callback_query.data.split(':', 1)[1])
    pubkey = user_data['sn'][i]['pubkey']
    user_data['sn'][i]['expires_soon'] = True
    pp.flush()
    return service_node(bot, update, user_data, i, 'Okay, I\'ll send you expiry notifications when _{}_ is close to expiry (48h, 24h, and 6h).'.format(pubkey))


def disable_expires_soon(bot, update, user_data):
    i = int(update.callback_query.data.split(':', 1)[1])
    pubkey = user_data['sn'][i]['pubkey']
    user_data['sn'][i]['expires_soon'] = False
    pp.flush()
    return service_node(bot, update, user_data, i, 'Okay, I\'ll stop sending you notifications when _{}_ is close to expiry.'.format(pubkey))


def wallets_menu(bot, update, user_data, reply_text=''):
    wallets = []

    if 'wallets' not in user_data:
        user_data['wallets'] = set()

    for w in sorted(user_data['wallets']):
        # button data can only be 64 bytes long, so truncate if longer
        tag = 'forget_wallet:{}'.format(w)
        if len(tag) > 64:
            tag = tag[0:64]
        wallets.append([InlineKeyboardButton(
            'Forget {}'.format(w),
            callback_data=tag)])

    wallets.append([InlineKeyboardButton('Add a wallet', callback_data='add_wallet'),
        InlineKeyboardButton('<< Main menu', callback_data='main')])

    w_menu = InlineKeyboardMarkup(wallets)
    if reply_text:
        reply_text += '\n\n'
    reply_text += 'If you send your wallet address(s) I can calculate your earned reward when your shared contribution service node earns a reward.'

    send_reply(bot, update, reply_text, reply_markup=w_menu)


def forget_wallet(bot, update, user_data):
    w = update.callback_query.data.split(':', 1)[1]
    if 'wallets' not in user_data:
        user_data['wallets'] = set()

    msgs = []
    remove = []
    for x in user_data['wallets']:
        if x.startswith(w):
            remove.append(x)
    for x in remove:
        user_data['wallets'].remove(x)
        msgs.append('Okay, I forgot about wallet _{}_.'.format(x))

    if msgs:
        pp.flush()
    else:
        msgs.append('I didn\'t know about that wallet in the first place!')

    return wallets_menu(bot, update, user_data, '\n'.join(msgs))


def add_wallet(bot, update, user_data):
    user_data['want_wallet'] = True
    pp.flush()
    msg = 'To register a wallet, just tell me the address.  You can either send the whole address or, if you prefer, just the first 7 or more characters of the wallet address.'
    send_reply(bot, update, msg)


def error(bot, update, error):
    """Log Errors caused by Updates."""
    logger.warning('Update "%s" caused error "%s"', update, error)


def help(bot, update, user_data):
    send_reply(bot, update, "Use /start to control this bot.")


def dispatch_query(bot, update, user_data):
    q = update.callback_query.data
    edit = True
    call = None
    if q == 'main':
        call = intro
    elif q == 'sns':
        call = service_nodes_menu
    elif q == 'sns_expiries':
        call = service_nodes_expiries
    elif q == 'status':
        call = status
    elif q == 'add_sn':
        call = service_node_add
    elif re.match(r'sn:\d+', q):
        call = service_node_menu
    elif re.match(r'refresh:\d+', q):
#        bot.delete_message(chat_id=update.callback_query.message.chat_id, message_id=update.callback_query.message.message_id)
        edit = False
        call = service_node_menu_inplace
    elif re.match(r'stop:\d+', q):
        call = stop_monitoring
    elif re.match(r'alias:\d+', q):
        call = add_alias
    elif re.match(r'del_alias:\d+', q):
        call = del_alias
    elif re.match(r'note:\d+', q):
        call = add_note
    elif re.match(r'del_note:\d+', q):
        call = del_note
    elif re.match(r'enable_reward:\d+', q):
        call = enable_reward_notify
    elif re.match(r'disable_reward:\d+', q):
        call = disable_reward_notify
    elif re.match(r'enable_expires_soon:\d+', q):
        call = enable_expires_soon
    elif re.match(r'disable_expires_soon:\d+', q):
        call = disable_expires_soon
    elif q == 'wallets':
        call = wallets_menu
    elif re.match(r'forget_wallet:\w+', q):
        call = forget_wallet
    elif q == 'add_wallet':
        call = add_wallet

    if edit:
        bot.edit_message_reply_markup(reply_markup=None,
                chat_id=update.callback_query.message.chat_id,
                message_id=update.callback_query.message.message_id)
    if call:
        return call(bot, update, user_data)


time_to_die = False
def loki_updater():
    global network_info, sn_states, time_to_die, updater
    expected_dereg_height = {}
    last = 0
    while not time_to_die:
        now = time.time()
        if now - last < 10:
            time.sleep(0.25)
            continue

        try:
            status = requests.get(NODE_URL + '/get_info', timeout=2).json()
            sns = requests.post(NODE_URL + '/json_rpc', json={"jsonrpc":"2.0","id":"0","method":"get_service_nodes"},
                    timeout=2).json()['result']['service_node_states']
        except Exception as e:
            print("An exception occured during loki stats fetching: {}".format(e))
            continue
        last = now
        network_info = status
        sn_states = { x['service_node_pubkey']: x for x in sns }
        for x in sns:
            expected_dereg_height[x['service_node_pubkey']] = x['registration_height'] + STAKE_BLOCKS

        if pp:
            try:
                data = pp.get_user_data()
                save = False
                for chatid, user_data in data.items():
                    if 'sn' not in user_data:
                        continue
                    my_addrs = set()
                    if 'wallets' in user_data and user_data['wallets']:
                        my_addrs = user_data['wallets']
                    for i in range(len(user_data['sn'])):
                        sn = user_data['sn'][i]
                        pubkey = sn['pubkey']
                        name = alias(sn)
                        sn_details_buttons = InlineKeyboardMarkup([[
                            InlineKeyboardButton('SN details', callback_data='sn:{}'.format(i)),
                            InlineKeyboardButton('lokiblocks.com', url='https://lokiblocks.com/service_node/{}'.format(pubkey))]])
                        if pubkey not in sn_states:
                            if 'notified_dereg' not in sn:
                                dereg_msg = ('📅 Service node _{}_ reached the end of its registration period and is no longer registered on the network.'.format(name)
                                        if pubkey in expected_dereg_height and expected_dereg_height[pubkey] <= network_info['height'] else
                                        '🛑 *UNEXPECTED DEREGISTRATION!* Service node _{}_ is no longer registered on the network! 😦'.format(name))
                                if send_message_or_shutup(updater.bot, chatid, dereg_msg, reply_markup=sn_details_buttons):
                                    sn['notified_dereg'] = True
                                    if 'complete' in sn:
                                        del sn['complete']
                                    sn['last_contributions'] = 0
                                    if 'expiry_notified' in sn:
                                        del sn['expiry_notified']
                                    save = True
                            continue
                        elif 'notified_dereg' in sn:
                            del sn['notified_dereg']
                            save = True

                        info = sn_states[pubkey]

                        if info['last_uptime_proof']:
                            proof_age = int(time.time() - info['last_uptime_proof'])
                            if proof_age >= PROOF_AGE_WARNING:
                                if 'notified_age' not in sn or proof_age - sn['notified_age'] > PROOF_AGE_REPEAT:
                                    if send_message_or_shutup(updater.bot, chatid,
                                            '⚠ *WARNING:* Service node _{}_ last uptime proof is *{}*'.format(name, uptime(info['last_uptime_proof'])),
                                            reply_markup=sn_details_buttons):
                                        sn['notified_age'] = proof_age
                                        save = True
                            elif 'notified_age' in sn:
                                if send_message_or_shutup(updater.bot, chatid,
                                        '😌 Service node _{}_ last uptime proof received (now *{}*)'.format(name, uptime(info['last_uptime_proof'])),
                                        reply_markup=sn_details_buttons):
                                    del sn['notified_age']
                                    save = True

                        just_completed = False
                        if 'complete' not in sn:
                            if 'last_contributions' not in sn or sn['last_contributions'] < info['total_contributed']:
                                pct = info['total_contributed'] / info['staking_requirement'] * 100
                                msg_part_a = ('{} Service node _{}_ is awaiting contributions.' if 'last_contributions' not in sn else
                                        '{} Service node _{}_ received a contribution.').format(moon_symbol(pct), name)

                                if send_message_or_shutup(updater.bot, chatid,
                                        msg_part_a + '  Total contributions: _{:.9f}_ (_{:.1f}%_ of required _{:.9f}_).  Additional contribution required: _{:.9f}_.'.format(
                                            info['total_contributed']*1e-9, pct, info['staking_requirement']*1e-9, (info['staking_requirement'] - info['total_contributed'])*1e-9),
                                        reply_markup=sn_details_buttons):
                                    sn['last_contributions'] = info['total_contributed']
                                    save = True

                            if info['total_contributed'] >= info['staking_requirement']:
                                if send_message_or_shutup(updater.bot, chatid,
                                        '💚 Service node _{}_ is now fully staked and active!'.format(name),
                                        reply_markup=sn_details_buttons):
                                    sn['complete'] = True
                                    save = True
                                just_completed = True



                        if 'expires_soon' in sn:
                            expires_at = info['registration_height'] + STAKE_BLOCKS
                            expires_in = expires_at - network_info['height']
                            expires_hours = expires_in / 30
                            notify_time = 6 if expires_hours <= 6 else 24 if expires_hours <= 24 else 48 if expires_hours <= 48 else None
                            if notify_time and expires_hours <= notify_time and ('expiry_notified' not in sn or sn['expiry_notified'] > notify_time):
                                expires_hours = round(expires_hours)
                                if send_message_or_shutup(updater.bot, chatid,
                                        '⏱ Service node _{}_ registration expires in about {:.0f} hour{} (block _{}_)'.format(
                                            name, expires_hours, '' if expires_hours == 1 else 's', expires_at),
                                        reply_markup=sn_details_buttons):
                                    sn['expiry_notified'] = notify_time
                                    save = True
                            elif notify_time is None and 'expiry_notified' in sn:
                                del sn['expiry_notified']
                                save = True

                        lrbh = info['last_reward_block_height']
                        if 'lrbh' not in sn:
                            sn['lrbh'] = lrbh
                            save = True
                        elif 'lrbh' in sn and lrbh > sn['lrbh']:
                            if 'rewards' in sn and sn['rewards'] and not just_completed and info['total_contributed'] >= info['staking_requirement']:
                                reward = 14 + 50 * 2**(-lrbh/64800)
                                my_rewards = []
                                if my_addrs and len(info['contributors']) > 1:
                                    for y in info['contributors']:
                                        if any(y['address'].startswith(x) for x in my_addrs):
                                            operator_reward = reward * info['portions_for_operator'] / 18446744073709551612.
                                            mine = (reward - operator_reward) * y['amount'] / info['staking_requirement']
                                            if y['address'] == info['operator_address']:
                                                mine += operator_reward
                                            my_rewards.append('*{:.3f} LOKI* (_{}...{}_)'.format(mine, y['address'][0:7], y['address'][-3:]))

                                if send_message_or_shutup(updater.bot, chatid,
                                        '💰 Service node _{}_ earned a reward of *{:.3f} LOKI* at height *{}*.'.format(name, reward, lrbh) + (
                                            '  Your share: ' + ', '.join(my_rewards) if my_rewards else '')):
                                    sn['lrbh'] = lrbh
                                    save = True
                            else:
                                sn['lrbh'] = lrbh
                                save = True


                if save:
                    pp.flush()
            except Exception as e:
                print("An exception occured during updating/notifications: {}".format(e))
                continue


loki_thread = None
def start_loki_update_thread():
    global loki_thread, network_info
    loki_thread = threading.Thread(target=loki_updater)
    loki_thread.start()
    while True:
        if network_info:
            print("Loki data fetched")
            return
        time.sleep(0.25)


def stop_loki_thread(signum, frame):
    global time_to_die, loki_thread
    time_to_die = True
    loki_thread.join()


def main():
    start_loki_update_thread()

    print("Starting bot")
    # Create the Updater and pass it your bot's token.
    global pp, updater
    pp = PicklePersistence(filename=PERSISTENCE_FILENAME, store_user_data=True, store_chat_data=False, on_flush=True)
    updater = Updater(TELEGRAM_TOKEN, persistence=pp,
            user_sig_handler=stop_loki_thread)

    # Get the dispatcher to register handlers
    dp = updater.dispatcher

    updater.dispatcher.add_handler(CommandHandler('start', start, pass_user_data=True))
    updater.dispatcher.add_handler(CommandHandler('help', help, pass_user_data=True))
    updater.dispatcher.add_handler(CallbackQueryHandler(dispatch_query, pass_user_data=True))
    updater.dispatcher.add_handler(MessageHandler(Filters.text, service_node_input, pass_user_data=True))

    # log all errors
    dp.add_error_handler(error)

    # Start the Bot
    updater.start_polling()

    print("Bot started")

    # Run the bot until you press Ctrl-C or the process receives SIGINT,
    # SIGTERM or SIGABRT. This should be used most of the time, since
    # start_polling() is non-blocking and will stop the bot gracefully.
    updater.idle()


if __name__ == '__main__':
    main()
