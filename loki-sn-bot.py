#!/usr/bin/python3

import threading
import time
import re
import requests
import logging
import traceback
import psycopg2, psycopg2.extras
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler, CallbackContext
from telegram.ext.dispatcher import run_async
from telegram.error import TelegramError

from loki_sn_bot_config import TELEGRAM_TOKEN, PGSQL_CONNECT, NODE_URL, TESTNET_NODE_URL, OWNER, EXTRA



# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

logger = logging.getLogger(__name__)

pgsql = None
updater = None

network_info = {}
sn_states = {}
testnet_sn_states = {}
testnet_network_info = {}


PROOF_AGE_WARNING = 3600 + 300  # 1 hour plus 5 minutes of grace time (uptime proofs can take slightly more than an hour)
PROOF_AGE_REPEAT = 600  # How often to repeat the alert
STAKE_BLOCKS = 720*30 + 20
TESTNET_STAKE_BLOCKS = 720*2 + 20


welcome_message = (
        'Hi!  I can give you loki service node information and send you alerts if the uptime proof for your service node(s) gets too long.  ' +
        'I can also optionally let you know when your service nodes earn a payment and when your service node is nearing expiry.' +
        ('\n\nThis bot is operated by ' + OWNER if OWNER else '') +
        ('\n\n' + EXTRA if EXTRA else '')
)

def lsr(h, testnet=False):
    if testnet:
        return 100
    elif h >= 235987:
        return 15000 + 24721 * 2**((101250-h)/129600.)
    else:
        return 10000 + 35000 * 2**((101250-h)/129600.)


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
    return sn['alias'] or sn['pubkey'][0:5] + '...' + sn['pubkey'][-3:]


def ago(seconds):
    return friendly_time(seconds) + ' ago'


def moon_symbol(pct):
    return '🌑' if pct < 26 else '🌒' if pct < 50 else '🌓' if pct < 75 else '🌔' if pct < 100 else '🌕'


def escape_markdown(text):
    return text.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")


def send_reply(update: Update, context: CallbackContext, message, reply_markup=None):
    chat_id = None
    if update.message:
        chat_id = update.message.chat.id
        update.message.reply_markdown(message,
                reply_markup=reply_markup,
                disable_web_page_preview=True)
    else:
        chat_id = update.callback_query.message.chat_id
        context.bot.send_message(
            chat_id=chat_id,
            text=message, parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup,
            disable_web_page_preview=True)


def send_message_or_shutup(bot, chatid, message, parse_mode=ParseMode.MARKDOWN, reply_markup=None):
    """Send a message to the bot.  If the message gives a 'bot was blocked by the user' error then
    we delete the user's service_nodes (to stop generating more messages)."""
    try:
        bot.send_message(chatid, message, parse_mode=parse_mode, reply_markup=reply_markup)
    except TelegramError as e:
        if 'bot was blocked by the user' in e.message:
            print("user {} blocked me; removing them from SN monitoring ({})".format(chatid, e), flush=True)

            pgsql.cursor().execute("DELETE FROM service_nodes WHERE uid = (SELECT id FROM users WHERE telegram_id = %s)", (chatid,))
        else:
            print("Error sending message to {}: {}".format(chatid, e), flush=True)
        return False

    return True


def get_uid(update, context):
    """Returns the user id in the pg database"""
    if 'uid' not in context.user_data:
        cur = pgsql.cursor()
        cur.execute("SELECT id FROM users WHERE telegram_id = %s", (update.effective_user.id,))
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO users (telegram_id) VALUES (%s) RETURNING id", (update.effective_user.id,))
            row = cur.fetchone()
        if row is None:
            return None
        context.user_data['uid'] = row[0]
    return context.user_data['uid']


def main_menu(update: Update, context: CallbackContext, reply='', last_button=InlineKeyboardButton('Status', callback_data='status')):
    choices = InlineKeyboardMarkup([
        [InlineKeyboardButton('Service node(s)', callback_data='sns'), InlineKeyboardButton('Wallet(s)', callback_data='wallets')],
        [last_button]
    ])

    need_flush = False
    for x in ('want_alias', 'want_note', 'want_wallet'):
        if x in context.user_data:
            del context.user_data[x]

    if reply:
        reply += '\n\n'

    uid = get_uid(update, context)
    cur = pgsql.cursor()
    cur.execute("SELECT COUNT(*) FROM service_nodes WHERE uid = %s", (uid,))
    num = cur.fetchone()[0]
    if num > 0:
        reply += "I am currently monitoring *{}* service node{} for you.".format(num, 's' if num != 1 else '')
    else:
        reply += "I am not currently monitoring any service nodes for you."

    send_reply(update, context, reply, reply_markup=choices)


@run_async
def start(update: Update, context: CallbackContext):
    return main_menu(update, context, welcome_message)


@run_async
def status(update: Update, context: CallbackContext):
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

    snbr = 0.5 * (28 + 100 * 2**(-h/64800))
    reply_text += 'Current SN stake requirement: *{:.2f}* LOKI\n'.format(lsr(h))
    reply_text += 'Current SN reward: *{:.4f}* LOKI\n'.format(snbr)

    cur = pgsql.cursor()
    cur.execute("SELECT COUNT(*) FROM (SELECT DISTINCT pubkey FROM service_nodes WHERE active AND NOT testnet) AS sns")
    monitored_sns = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM (SELECT DISTINCT users.id FROM users JOIN service_nodes ON uid = users.id WHERE active AND NOT testnet) AS usrs")
    active_users = cur.fetchone()[0]

    reply_text += 'I am currently monitoring *{}* active service nodes (*{:.1f}%*) on behalf of *{}* users.'.format(
            monitored_sns, 100 * monitored_sns / (active + waiting), active_users)

    return main_menu(update, context, reply_text, last_button=InlineKeyboardButton('<< Main menu', callback_data='main'))


def sn_status_icon(pubkey):
    sns, ninfo, tsns, tninfo = sn_states, network_info, testnet_sn_states, testnet_network_info
    status_icon, prefix = '🛑', ''
    if pubkey in sns:
        info = sns[pubkey]
        height = ninfo['height']
        stake_blocks = STAKE_BLOCKS
    elif pubkey in tsns:
        info = tsns[pubkey]
        height = tninfo['height']
        stake_blocks = TESTNET_STAKE_BLOCKS
        prefix = '🚧'
    else:
        return status_icon

    proof_age = int(time.time() - info['last_uptime_proof'])
    if proof_age >= PROOF_AGE_WARNING:
        status_icon = '⚠'
    elif info['total_contributed'] < info['staking_requirement']:
        status_icon = moon_symbol(info['total_contributed'] / info['staking_requirement'] * 100)
    elif info['registration_height'] + stake_blocks - height < 48*30:
        status_icon = '⏱'
    else:
        status_icon = '💚'

    return prefix + status_icon


@run_async
def service_nodes_menu(update: Update, context: CallbackContext, reply_text=''):
    buttons = []
    cur = pgsql.cursor(cursor_factory=psycopg2.extras.DictCursor)
    uid = get_uid(update, context)
    cur.execute("SELECT * FROM service_nodes WHERE uid = %s ORDER BY testnet, alias, pubkey", (uid,))
    for sn in cur:
        status_icon = sn_status_icon(sn['pubkey'])
        shortpub = sn['pubkey'][0:5] + '…' + sn['pubkey'][-2:]
        snbutton = InlineKeyboardButton(
                status_icon + ' ' + ('{} ({})'.format(sn['alias'], shortpub) if sn['alias'] else shortpub),
                callback_data='sn:{}'.format(sn['id']))
        if buttons and len(buttons[-1]) == 1:
            buttons[-1].append(snbutton)
        else:
            buttons.append([snbutton])

    buttons.append([InlineKeyboardButton('Add a service node', callback_data='add_sn'),
        InlineKeyboardButton('Show expirations', callback_data='sns_expiries')]);
    buttons.append([
        InlineKeyboardButton('<< Main menu', callback_data='main')])

    sn_menu = InlineKeyboardMarkup(buttons)
    if reply_text:
        reply_text += '\n\n'
    reply_text += 'View an existing service node, or add a new one?'

    send_reply(update, context, reply_text, reply_markup=sn_menu)


@run_async
def service_nodes_expiries(update: Update, context: CallbackContext):
    sns, ninfo, tsns, tninfo = sn_states, network_info, testnet_sn_states, testnet_network_info
    mysns, mytsns = [], []
    cur = pgsql.cursor(cursor_factory=psycopg2.extras.DictCursor)
    uid = get_uid(update, context)
    cur.execute("SELECT * FROM service_nodes WHERE uid = %s ORDER BY alias, pubkey", (uid,))
    for sn in cur:
        pubkey = sn['pubkey']
        row = { 'sn': sn }
        row['icon'] = sn_status_icon(pubkey)
        if pubkey in sns:
            row['info'] = sns[pubkey]
        elif pubkey in tsns:
            row['info'] = tsns[pubkey]

        (mytsns if sn['testnet'] else mysns).append(row)

    mysns.sort(key=lambda s: s['info']['registration_height'] if 'info' in s else 0)
    mytsns.sort(key=lambda s: s['info']['registration_height'] if 'info' in s else 0)

    msg = '*Service nodes expirations:*\n'
    for sn in mysns:
        msg += '{} {}: '.format(sn['icon'], alias(sn['sn']))
        if 'info' in sn:
            expiry_block = sn['info']['registration_height'] + STAKE_BLOCKS
            msg += 'Block _{}_ (_{}_)\n'.format(
                    expiry_block, friendly_time(120 * (expiry_block + 1 - ninfo['height'])))
        else:
            msg += 'Expired/deregistered\n'

    if mytsns:
        msg += '\n*Testnet service nodes expirations:*\n'
        for sn in mytsns:
            msg += '{} {}: '.format(sn['icon'], alias(sn['sn']))
            if 'info' in sn:
                expiry_block = sn['info']['registration_height'] + TESTNET_STAKE_BLOCKS
                msg += 'Block _{}_ (_{}_)\n'.format(
                        expiry_block, friendly_time(120 * (expiry_block + 1 - tninfo['height'])))
            else:
                msg += 'Expired/deregistered\n'

    service_nodes_menu(update, context, reply_text=msg)


@run_async
def service_node_add(update: Update, context: CallbackContext):
    send_reply(update, context, 'Okay, send me the public key of the service node to add', None)


@run_async
def service_node_menu(update: Update, context: CallbackContext, ):
    snid = int(update.callback_query.data.split(':', 1)[1])
    return service_node(update, context, snid)


@run_async
def service_node_menu_inplace(update: Update, context: CallbackContext):
    snid = int(update.callback_query.data.split(':', 1)[1])
    return service_node(update, context, snid,
            callback=lambda msg, reply_markup: context.bot.edit_message_text(
                text=msg, parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup, chat_id=update.callback_query.message.chat_id,
                message_id=update.callback_query.message.message_id))


@run_async
def service_node_input(update: Update, context: CallbackContext):

    uid = get_uid(update, context)

    user_data = context.user_data
    if 'want_note' in user_data:
        snid = user_data['want_note']
        del user_data['want_note']
        pgsql.cursor().execute("UPDATE service_nodes SET note = %s WHERE id = %s AND uid = %s",
                (update.message.text, snid, uid))
        return service_node(update, context, snid, 'Updated note for _{alias}_.  Current status:')

    elif 'want_alias' in user_data:
        snid = user_data['want_alias']
        alias = update.message.text.replace("*", "").replace("_", "").replace("[", "").replace("`", "")
        del user_data['want_alias']
        cur = pgsql.cursor()
        cur.execute("UPDATE service_nodes SET alias = %s WHERE id = %s AND uid = %s RETURNING pubkey",
                (alias, snid, uid))
        return service_node(update, context, snid, 'Okay, I\'ll now refer to service node _{sn[pubkey]}_ as _{sn[alias]}_.  Current status:')

    elif 'want_wallet' in user_data:
        wallet = update.message.text
        if (    not re.match('^L[4-9A-E][1-9A-HJ-NP-Za-km-z]{5,93}$', wallet) and
                not re.match('^T[6-9A-G][1-9A-HJ-NP-Za-km-z]{5,95}$', wallet)):
            send_reply(update, context, 'That doesn\'t look like a valid primary wallet address.  Send me at least the first 7 characters of your primary wallet address')
            return
        del user_data['want_wallet']
        pgsql.cursor().execute("INSERT INTO wallet_prefixes (uid, wallet) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (uid, wallet))
        return wallets_menu(update, context, 'Added {}wallet _{}_.  I\'ll now calculate your share of shared contribution service node rewards.'.format(
            '*testnet* ' if wallet[0] == 'T' else '', wallet))


    pubkey = update.message.text

    if not re.match('^[0-9a-f]{64}$', pubkey):
        send_reply(update, context, 'Sorry, I didn\'t understand your message.  Send me a service node public key or use /start')
        return

    cur = pgsql.cursor()
    cur.execute("SELECT id FROM service_nodes WHERE uid = %s AND pubkey = %s",
            (uid, pubkey))
    found = cur.fetchone()

    if found:
        snid = found[0]
        reply_text = 'I am _already_ monitoring service node _{sn[pubkey]}_ for you.  Current status:'

    else:
        active, complete, lrbh, testnet = False, False, None, False
        sns, tsns = sn_states, testnet_sn_states
        if pubkey in sns:
            active = True
            complete = sns[pubkey]['total_contributed'] >= sns[pubkey]['staking_requirement']
            lrbh = sns[pubkey]['last_reward_block_height']
            reply_text = 'Okay, I\'m now monitoring service node _{sn[pubkey]}_ for you.  Current status:'
        elif pubkey in tsns:
            active = True
            testnet = True
            complete = tsns[pubkey]['total_contributed'] >= tsns[pubkey]['staking_requirement']
            lrbh = tsns[pubkey]['last_reward_block_height']
            reply_text = 'Okay, I\'m now monitoring *testnet* service node _{sn[pubkey]}_ for you.  Current status:'
        else:
            reply_text = 'Service node _{sn[pubkey]}_ isn\'t currently registered on the network, but I\'ll start monitoring it for you once it appears.'.format(pubkey)
        cur.execute("INSERT INTO service_nodes (uid, pubkey, active, complete, last_reward_block_height, testnet) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (uid, pubkey, active, complete, lrbh, testnet))
        snid = cur.fetchone()[0]

    return service_node(update, context, snid, reply_text)


def service_node(update: Update, context: CallbackContext, snid, reply_text = '', callback = None):
    uid = get_uid(update, context)
    cur = pgsql.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT * FROM service_nodes WHERE id = %s AND uid = %s", (snid, uid))
    sn = cur.fetchone()
    reply_text = reply_text.format(sn=sn, alias=alias(sn))
    if reply_text:
        reply_text += '\n\n'
    else:
        reply_text = 'Current status of service node _{}_:\n\n'.format(alias(sn))

    pubkey = sn['pubkey']
    if sn['testnet']:
        sns, ninfo = testnet_sn_states, testnet_network_info
        reply_text += '🚧 This is a *testnet* service node! 🚧\n'
    else:
        sns, ninfo = sn_states, network_info

    if sn['note']:
        reply_text += 'Note: ' + escape_markdown(sn['note']) + '\n'

    if pubkey not in sns:
        if sn['alias']:
            reply_text += 'Service node _{}_ is not registered\n'.format(pubkey)
        else:
            reply_text += 'Not registered\n'
    else:
        info = sns[pubkey]
        height = ninfo['height']

        reply_text += 'Public key: _{}_\n'.format(pubkey)

        reply_text += 'Last uptime proof: ' + uptime(info['last_uptime_proof']) + '\n'

        expiry_block = info['registration_height'] + STAKE_BLOCKS
        reg_expiry = 'Block *{}* (approx. {})\n'.format(expiry_block, friendly_time(120 * (expiry_block + 1 - height)));

        my_stakes = []
        cur2 = pgsql.cursor()
        cur2.execute("SELECT wallet FROM wallet_prefixes WHERE uid = %s", (uid,))
        for row in cur2:
            w = row[0]
            for y in info['contributors']:
                if y['address'].startswith(w):
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
            for sni in list(sns.values()):
                if sni['total_contributed'] >= sni['staking_requirement'] and sni['last_reward_block_height'] < info['last_reward_block_height']:
                    blocks_to_go += 1
            reply_text += 'Next reward in *{}* blocks (approx. {})\n'.format(blocks_to_go, friendly_time(blocks_to_go * 120))

        reply_text += 'Reward notifications: *' + ('en' if sn['rewards'] else 'dis') + 'abled*\n'
        reply_text += 'Close-to-expiry notifications: *' + ('en' if sn['expires_soon'] else 'dis') + 'abled*\n'

    menu = InlineKeyboardMarkup([
        [InlineKeyboardButton('Refresh', callback_data='refresh:{}'.format(snid)),
         InlineKeyboardButton('View on lokiblocks.com', url='https://lokiblocks.com/service_node/{}'.format(pubkey))],
        [InlineKeyboardButton('Stop monitoring', callback_data='stop:{}'.format(snid))],
        [InlineKeyboardButton('Update alias', callback_data='alias:{}'.format(snid)),
            InlineKeyboardButton('Remove alias', callback_data='del_alias:{}'.format(snid))]
            if sn['alias'] else
        [InlineKeyboardButton('Set alias', callback_data='alias:{}'.format(snid))],
        [InlineKeyboardButton('Change note', callback_data='note:{}'.format(snid)),
         InlineKeyboardButton('Delete note', callback_data='del_note:{}'.format(snid))]
            if sn['note'] else
        [InlineKeyboardButton('Add custom note', callback_data='note:{}'.format(snid))],
        [InlineKeyboardButton(('Disable' if sn['rewards'] else 'Enable') + ' reward notifications',
            callback_data=('dis' if sn['rewards'] else 'en') + 'able_reward:{}'.format(snid))],
        [InlineKeyboardButton(('Disable' if sn['expires_soon'] else 'Enable') + ' close-to-expiry notifications',
            callback_data=('dis' if sn['expires_soon'] else 'en') + 'able_expires_soon:{}'.format(snid))],
        [InlineKeyboardButton('< Service nodes', callback_data='sns'), InlineKeyboardButton('<< Main menu', callback_data='main')]
        ])

    if not callback:
        callback = lambda msg, markup: send_reply(update, context, msg, markup)
    callback(reply_text, menu)


@run_async
def stop_monitoring(update: Update, context: CallbackContext):
    uid = get_uid(update, context)
    snid = int(update.callback_query.data.split(':', 1)[1])
    cur = pgsql.cursor()
    cur.execute("DELETE FROM service_nodes WHERE uid = %s AND id = %s RETURNING pubkey", (uid, snid))
    found = cur.fetchone()
    msg = ("Okay, I'm not longer monitoring service node _{}_ for you.".format(found[0]) if found else
            "I couldn't find that service node; please try again")
    return service_nodes_menu(update, context, msg)


def request_sn_field(update: Update, context: CallbackContext, field, send_fmt, current_fmt):
    uid = get_uid(update, context)
    snid = int(update.callback_query.data.split(':', 1)[1])
    cur = pgsql.cursor()
    cur.execute("SELECT pubkey, alias, "+field+" FROM service_nodes WHERE uid = %s AND id = %s", (uid, snid))
    found = cur.fetchone()
    if found:
        context.user_data['want_'+field] = snid
        msg = send_fmt.format(alias({ 'pubkey': found[0], 'alias': found[1] }), pubkey=found[0])
        if found[2]:
            msg += '\n\n' + current_fmt.format(found[2], escaped=escape_markdown(found[2]))
    else:
        msg = "I couldn't find that service node; please try again"
    send_reply(update, context, msg)


def set_sn_field(update: Update, context: CallbackContext, field, value, success):
    uid = get_uid(update, context)
    snid = int(update.callback_query.data.split(':', 1)[1])
    cur = pgsql.cursor()
    cur.execute("UPDATE service_nodes SET "+field+" = %s WHERE uid = %s AND id = %s RETURNING pubkey, alias", (value, uid, snid))
    found = cur.fetchone()
    msg = (success.format(alias({ 'pubkey': found[0], 'alias': found[1] })) if found else
            "I couldn't find that service node; please try again")
    service_node(update, context, snid, msg)


@run_async
def ask_note(update: Update, context: CallbackContext):
    request_sn_field(update, context, 'note',
            "Send me a custom note to set for service node _{}_.",
            "The current note is: {escaped}")


@run_async
def del_note(update: Update, context: CallbackContext):
    set_sn_field(update, context, 'note', None, 'Removed note for service node _{}_.')


@run_async
def ask_alias(update: Update, context: CallbackContext):
    request_sn_field(update, context, 'alias',
            "Send me an alias to use for this service node instead of the public key (_{pubkey}_).",
            "The current alias is: {}")


@run_async
def del_alias(update: Update, context: CallbackContext):
    set_sn_field(update, context, 'alias', None, 'Removed alias for service node _{}_.')


@run_async
def enable_reward_notify(update: Update, context: CallbackContext, ):
    set_sn_field(update, context, 'rewards', True,
            "Okay, I'll start sending you block reward notifications for _{}_.")


@run_async
def disable_reward_notify(update: Update, context: CallbackContext):
    set_sn_field(update, context, 'rewards', False,
            "Okay, I'll no longer send you block reward notifications for _{}_.")


@run_async
def enable_expires_soon(update: Update, context: CallbackContext):
    set_sn_field(update, context, 'expires_soon', True,
            "Okay, I'll send you expiry notifications when _{}_ is close to expiry (48h, 24h, and 6h).")


@run_async
def disable_expires_soon(update: Update, context: CallbackContext):
    set_sn_field(update, context, 'expires_soon', False,
            "Okay, I'll stop sending you notifications when _{}_ is close to expiry.")


@run_async
def wallets_menu(update: Update, context: CallbackContext, reply_text=''):
    uid = get_uid(update, context)

    wallets = []

    cur = pgsql.cursor()
    cur.execute("SELECT wallet from wallet_prefixes WHERE uid = %s ORDER BY wallet", (uid,))
    for row in cur:
        w = row[0]
        # button data can only be 64 bytes long, so truncate if longer
        tag = 'forget_wallet:{}'.format(w)
        if len(tag) > 64:
            tag = tag[0:64]
        prefix = '🚧' if w.startswith('T') else ''

        wallets.append([InlineKeyboardButton(
            'Forget {}{}'.format(prefix, w),
            callback_data=tag)])

    wallets.append([InlineKeyboardButton('Add a wallet', callback_data='ask_wallet'),
        InlineKeyboardButton('<< Main menu', callback_data='main')])

    w_menu = InlineKeyboardMarkup(wallets)
    if reply_text:
        reply_text += '\n\n'
    reply_text += 'If you send your wallet address(es) I can calculate your specific reward and show you your stake (for shared contribution service nodes).'

    send_reply(update, context, reply_text, reply_markup=w_menu)


@run_async
def forget_wallet(update: Update, context: CallbackContext, ):
    w = update.callback_query.data.split(':', 1)[1]

    uid = get_uid(update, context)
    msgs = []
    remove = []
    cur = pgsql.cursor()
    cur.execute("SELECT wallet from wallet_prefixes WHERE uid = %s ORDER BY wallet", (uid,))
    for row in cur:
        if row[0].startswith(w):
            remove.append(row[0])
    cur.execute("DELETE FROM wallet_prefixes WHERE uid = %s AND wallet IN %s RETURNING wallet",
            (uid, tuple(remove)))
    for x in cur:
        msgs.append('Okay, I forgot about wallet _{}_.'.format(x[0]))

    if not msgs:
        msgs.append('I didn\'t know about that wallet in the first place!')

    return wallets_menu(update, context, '\n'.join(msgs))


@run_async
def ask_wallet(update: Update, context: CallbackContext):
    context.user_data['want_wallet'] = True
    msg = 'To register a wallet, just tell me the address.  You can either send the whole address or, if you prefer, just the first 7 (or more) characters of the wallet address.'
    send_reply(update, context, msg)


def error(update: Update, context: CallbackContext):
    """Log Errors caused by Updates."""
    logger.warning('Update "%s" caused error "%s"', update, context.error)


@run_async
def help(update: Update, context: CallbackContext):
    send_reply(update, context, "Use /start to control this bot.")


@run_async
def dispatch_query(update: Update, context: CallbackContext):
    q = update.callback_query.data
    edit = True
    call = None
    if q == 'main':
        call = start
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
        call = ask_alias
    elif re.match(r'del_alias:\d+', q):
        call = del_alias
    elif re.match(r'note:\d+', q):
        call = ask_note
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
    elif q == 'ask_wallet':
        call = ask_wallet

    if edit:
        context.bot.edit_message_reply_markup(reply_markup=None,
                chat_id=update.callback_query.message.chat_id,
                message_id=update.callback_query.message.message_id)
    if call:
        return call(update, context)


def update_sn_fields(uid, snid, **kwargs):
    cur = pgsql.cursor()
    sets, vals = [], []
    for k, v in kwargs.items():
        sets.append(k + ' = %s')
        vals.append(v)
    vals += (snid, uid)
    cur.execute("UPDATE service_nodes SET " + ", ".join(sets) + " WHERE id = %s AND uid = %s", tuple(vals))


time_to_die = False
def loki_updater():
    global network_info, sn_states, testnet_network_info, testnet_sn_states, time_to_die, updater
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
        sns = { x['service_node_pubkey']: x for x in sns }
        sn_states, network_info = sns, status

        testnet = False
        if TESTNET_NODE_URL:
            try:
                tstatus = requests.get(TESTNET_NODE_URL + '/get_info', timeout=2).json()
                tsns = requests.post(TESTNET_NODE_URL + '/json_rpc', json={"jsonrpc":"2.0","id":"0","method":"get_service_nodes"},
                        timeout=2).json()['result']['service_node_states']
                tsns = { x['service_node_pubkey']: x for x in tsns }
                testnet_sn_states, testnet_network_info = tsns, tstatus
                testnet = True
            except Exception as e:
                print("An exception occured during loki testnet stats fetching: {}; ignoring the error".format(e))

        if testnet:
            for pubkey, x in tsns.items():
                expected_dereg_height[pubkey] = x['registration_height'] + TESTNET_STAKE_BLOCKS
        for pubkey, x in sns.items():
            expected_dereg_height[pubkey] = x['registration_height'] + STAKE_BLOCKS

        if not hasattr(updater, 'bot'):
            print("no bot yet!")
            continue
        try:
            cur = pgsql.cursor(cursor_factory=psycopg2.extras.DictCursor)
            wallets = {}
            cur.execute("SELECT uid, wallet FROM wallet_prefixes")
            for row in cur:
                if row[0] not in wallets:
                    wallets[row[0]] = []
                wallets[row[0]].append(row[1])

            inactive = []
            cur.execute("SELECT users.telegram_id, users.discord_id, service_nodes.* FROM users JOIN service_nodes ON uid = users.id ORDER BY uid")
            for sn in cur:
                if not sn['telegram_id']:
                    continue  # FIXME
                uid = sn['uid']
                chatid = sn['telegram_id']
                snid = sn['id']
                pubkey = sn['pubkey']
                name = alias(sn)
                netheight = tstatus['height'] if sn['testnet'] else status['height']
                sn_details_buttons = InlineKeyboardMarkup([[
                    InlineKeyboardButton('SN details', callback_data='sn:{}'.format(snid)),
                    InlineKeyboardButton('lokiblocks.com', url='https://lokiblocks.com/service_node/{}'.format(pubkey))]])
                if pubkey not in sns and pubkey not in tsns:
                    if not sn['notified_dereg']:
                        dereg_msg = ('📅 Service node _{}_ reached the end of its registration period and is no longer registered on the network.'.format(name)
                                if pubkey in expected_dereg_height and expected_dereg_height[pubkey] <= netheight else
                                '🛑 *UNEXPECTED DEREGISTRATION!* Service node _{}_ is no longer registered on the network! 😦'.format(name))
                        if send_message_or_shutup(updater.bot, chatid, dereg_msg, reply_markup=sn_details_buttons):
                            update_sn_fields(uid, snid, active=False, notified_dereg=True, complete=False, last_contributions=0, expiry_notified=None)
                    elif sn['active']:
                        update_sn_fields(uid, snid, active=False)

                    continue
                elif sn['notified_dereg'] or not sn['active']:
                    update_sn_fields(uid, snid, active=True, notified_dereg=False)

                if pubkey in sns:
                    info = sns[pubkey]
                    prefix = ''
                else:
                    info = tsns[pubkey]
                    prefix = '🚧'

                if info['last_uptime_proof']:
                    proof_age = int(time.time() - info['last_uptime_proof'])
                    if proof_age >= PROOF_AGE_WARNING:
                        if not sn['notified_age'] or proof_age - sn['notified_age'] > PROOF_AGE_REPEAT:
                            if send_message_or_shutup(updater.bot, chatid,
                                    prefix+'⚠ *WARNING:* Service node _{}_ last uptime proof is *{}*'.format(name, uptime(info['last_uptime_proof'])),
                                    reply_markup=sn_details_buttons):
                                update_sn_fields(uid, snid, notified_age=proof_age)
                    elif sn['notified_age']:
                        if send_message_or_shutup(updater.bot, chatid,
                                prefix+'😌 Service node _{}_ last uptime proof received (now *{}*)'.format(name, uptime(info['last_uptime_proof'])),
                                reply_markup=sn_details_buttons):
                            update_sn_fields(uid, snid, notified_age=None)

                just_completed = False
                if not sn['complete']:
                    if not sn['last_contributions'] or sn['last_contributions'] < info['total_contributed']:
                        pct = info['total_contributed'] / info['staking_requirement'] * 100
                        msg_part_a = ('{} Service node _{}_ is awaiting contributions.' if not sn['last_contributions'] else
                                '{} Service node _{}_ received a contribution.').format(moon_symbol(pct), name)

                        if send_message_or_shutup(updater.bot, chatid,
                                prefix + msg_part_a + '  Total contributions: _{:.9f}_ (_{:.1f}%_ of required _{:.9f}_).  Additional contribution required: _{:.9f}_.'.format(
                                    info['total_contributed']*1e-9, pct, info['staking_requirement']*1e-9, (info['staking_requirement'] - info['total_contributed'])*1e-9),
                                reply_markup=sn_details_buttons):
                            update_sn_fields(uid, snid, last_contributions=info['total_contributed'])

                    if info['total_contributed'] >= info['staking_requirement']:
                        if send_message_or_shutup(updater.bot, chatid,
                                prefix+'💚 Service node _{}_ is now fully staked and active!'.format(name),
                                reply_markup=sn_details_buttons):
                            update_sn_fields(uid, snid, complete=True)
                        just_completed = True



                if sn['expires_soon']:
                    expires_at = info['registration_height'] + (TESTNET_STAKE_BLOCKS if sn['testnet'] else STAKE_BLOCKS)
                    expires_in = expires_at - netheight
                    expires_hours = expires_in / 30
                    notify_time = 6 if expires_hours <= 6 else 24 if expires_hours <= 24 else 48 if expires_hours <= 48 else None
                    if notify_time and expires_hours <= notify_time and (not sn['expiry_notified'] or sn['expiry_notified'] > notify_time):
                        expires_hours = round(expires_hours)
                        if send_message_or_shutup(updater.bot, chatid,
                                prefix+'⏱ Service node _{}_ registration expires in about {:.0f} hour{} (block _{}_)'.format(
                                    name, expires_hours, '' if expires_hours == 1 else 's', expires_at),
                                reply_markup=sn_details_buttons):
                            update_sn_fields(uid, snid, expiry_notified=notify_time)
                    elif notify_time is None and sn['expiry_notified']:
                        update_sn_fields(uid, snid, expiry_notified=None)

                lrbh = info['last_reward_block_height']
                if not sn['last_reward_block_height']:
                    update_sn_fields(uid, snid, last_reward_block_height=lrbh)
                elif sn['last_reward_block_height'] and lrbh > sn['last_reward_block_height']:
                    if sn['rewards'] and not just_completed and info['total_contributed'] >= info['staking_requirement']:
                        reward = 14 + 50 * 2**(-lrbh/64800)
                        my_rewards = []
                        if sn['uid'] in wallets and len(info['contributors']) > 1:
                            for y in info['contributors']:
                                if any(y['address'].startswith(x) for x in wallets[sn['uid']]):
                                    operator_reward = reward * info['portions_for_operator'] / 18446744073709551612.
                                    mine = (reward - operator_reward) * y['amount'] / info['staking_requirement']
                                    if y['address'] == info['operator_address']:
                                        mine += operator_reward
                                    my_rewards.append('*{:.3f} LOKI* (_{}...{}_)'.format(mine, y['address'][0:7], y['address'][-3:]))

                        if send_message_or_shutup(updater.bot, chatid,
                                prefix+'💰 Service node _{}_ earned a reward of *{:.3f} LOKI* at height *{}*.'.format(name, reward, lrbh) + (
                                    '  Your share: ' + ', '.join(my_rewards) if my_rewards else '')):
                            update_sn_fields(uid, snid, last_reward_block_height=lrbh)
                    else:
                        update_sn_fields(uid, snid, last_reward_block_height=lrbh)


        except Exception as e:
            print("An exception occured during updating/notifications: {}".format(e))
            import sys
            traceback.print_exc(file=sys.stdout)
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
    global pgsql, updater
    pgsql = psycopg2.connect(**PGSQL_CONNECT)
    pgsql.autocommit = True

    start_loki_update_thread()

    print("Starting bot")

    # Create the Updater and pass it your bot's token.
    updater = Updater(TELEGRAM_TOKEN, workers=4, user_sig_handler=stop_loki_thread, use_context=True)

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
