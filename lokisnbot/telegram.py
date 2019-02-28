# Telegram-specific bits for loki-sn-bot

import re
import time
import requests

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, Update, ChatAction
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler, CallbackContext
from telegram.ext.dispatcher import run_async
from telegram.error import TelegramError

import lokisnbot
from . import pgsql
from .constants import *
from .util import friendly_time, ago, explorer, escape_markdown
from .servicenode import ServiceNode, lsr

updater = None

def send_reply(update: Update, context: CallbackContext, message, reply_markup=None, dead_end=False):
    """Sends a reply.  reply_markup can be used to append buttons; dead_end can be used instead of
    reply_markup to add just a '<< Main menu' button."""
    if dead_end and not reply_markup:
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton('<< Main menu', callback_data='main')]])

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


def main_menu(update: Update, context: CallbackContext, reply='', last_button=InlineKeyboardButton('Status', callback_data='status'), testnet_buttons=False):
    choices = [
        [InlineKeyboardButton('Service node(s)', callback_data='sns'), InlineKeyboardButton('Wallet(s)', callback_data='wallets')],
    ]
    if testnet_buttons:
        testnet_status, testnet_faucet = bool(lokisnbot.config.TESTNET_NODE_URL), bool(lokisnbot.config.TESTNET_WALLET_URL and lokisnbot.config.TESTNET_FAUCET_AMOUNT)
        if testnet_status or testnet_faucet:
            choices.append([])
            if testnet_status:
                choices[-1].append(InlineKeyboardButton('Testnet status', callback_data='testnet_status'))
            if testnet_faucet:
                choices[-1].append(InlineKeyboardButton('Testnet faucet', callback_data='testnet_faucet'))
    choices.append([last_button])

    need_flush = False
    for x in ('want_alias', 'want_note', 'want_wallet', 'want_faucet_address', 'want_add_sn'):
        if x in context.user_data:
            del context.user_data[x]

    if reply:
        reply += '\n\n'

    uid = get_uid(update, context)
    cur = pgsql.cursor()
    cur.execute("SELECT COUNT(*), testnet FROM service_nodes WHERE uid = %s GROUP BY testnet", (uid,))
    mainnet, testnet = 0, 0
    for row in cur:
        if row[1]:
            testnet = row[0]
        else:
            mainnet = row[0]

    if mainnet > 0 or testnet > 0:
        reply += "I am currently monitoring *{}* service node{}".format(mainnet, 's' if mainnet != 1 else '')
        if testnet > 0:
            reply += " and *{}* testnet service node{}".format(testnet, 's' if testnet != 1 else '')
        reply += " for you."
    else:
        reply += "I am not currently monitoring any service nodes for you."

    send_reply(update, context, reply, reply_markup=InlineKeyboardMarkup(choices))


@run_async
def start(update: Update, context: CallbackContext):
    return main_menu(update, context, lokisnbot.config.WELCOME, testnet_buttons=True)


@run_async
def status(update: Update, context: CallbackContext, testnet=False):
    sns = lokisnbot.testnet_sn_states if testnet else lokisnbot.sn_states
    active, waiting, old_proof = 0, 0, 0
    now = int(time.time())
    for sn in sns.values():
        if sn['total_contributed'] < sn['staking_requirement']:
            waiting += 1
        else:
            active += 1
        if sn['last_uptime_proof'] and now - sn['last_uptime_proof'] > PROOF_AGE_WARNING:
            old_proof += 1

    h = (lokisnbot.testnet_network_info if testnet else lokisnbot.network_info)['height']
    reply_text = '🚧 *Testnet* 🚧\n' if testnet else ''
    reply_text += 'Network height: *{}*\n'.format(h);
    reply_text += 'Service nodes: *{}* _(active)_ + *{}* _(awaiting contribution)_\n'.format(active, waiting)
    reply_text += '*{}* service node'.format(old_proof) + (' has uptime proof' if old_proof == 1 else 's have uptime proofs') + ' > 1h5m\n';

    snbr = 0.5 * (28 + 100 * 2**(-h/64800))
    reply_text += 'Current SN stake requirement: *{:.2f}* LOKI\n'.format(lsr(h, testnet=testnet))
    reply_text += 'Current SN reward: *{:.4f}* LOKI\n'.format(snbr)

    testnet_clause = "testnet" if testnet else "NOT testnet"
    cur = pgsql.cursor()
    cur.execute("SELECT COUNT(*) FROM (SELECT DISTINCT pubkey FROM service_nodes WHERE active AND "+testnet_clause+") AS sns")
    monitored_sns = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM (SELECT DISTINCT users.id FROM users JOIN service_nodes ON uid = users.id WHERE active AND "+testnet_clause+") AS usrs")
    active_users = cur.fetchone()[0]

    reply_text += 'I am currently monitoring *{}* active {}service nodes (*{:.1f}%*) on behalf of *{}* users.'.format(
            monitored_sns, "*testnet* " if testnet else "", 100 * monitored_sns / (active + waiting), active_users)

    return main_menu(update, context, reply_text, last_button=InlineKeyboardButton('<< Main menu', callback_data='main'))


def testnet_status(update: Update, context: CallbackContext):
    return status(update, context, testnet=True)


def faucet_was_recently_used(update: Update, context: CallbackContext):
    cur = pgsql.cursor()
    cur.execute("SELECT faucet_last_used FROM users WHERE id = %s", (get_uid(update, context),))
    last_used = cur.fetchone()[0]

    now = int(time.time())
    if last_used and last_used > now - 86400:
        send_reply(update, context, dead_end=True,
                message="🤔 It appears that you have already used the faucet recently.  You need to wait another {} before you can use it again.".format(
                    friendly_time(86400 - (now - last_used))))
        return True
    return False


@run_async
def testnet_faucet(update: Update, context: CallbackContext):
    if faucet_was_recently_used(update, context):
        return
    context.user_data['want_faucet_address'] = True
    send_reply(update, context, "So you want some *testnet LOKI*!  You've come to the right place: just send me your testnet address and I'll send some your way:")


@run_async
def turn_faucet(update: Update, context: CallbackContext):
    uid = get_uid(update, context)
    if faucet_was_recently_used(update, context):
        del context.user_data['want_faucet_address']
        return

    wallet = update.message.text
    if re.match('^L[4-9A-E][1-9A-HJ-NP-Za-km-z]{93}$', wallet):
        send_reply(update, context, "🤣 Nice try, but I don't have any mainnet LOKI.  Send me a _testnet_ wallet address instead")
    elif any(re.match(x, wallet) for x in (
        '^T[6-9A-G][1-9A-HJ-NP-Za-km-z]{95}$', # main addr
        '^T[GHJ-NP-R][1-9A-HJ-NP-Za-km-z]{106}$', # integrated
        '^T[R-Zab][1-9A-HJ-NP-Za-km-z]{95}$', # subaddress
        )):

        del context.user_data['want_faucet_address']
        context.bot.send_chat_action(chat_id=update.message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
        error = None
        try:
            transfer = requests.post(lokisnbot.config.TESTNET_WALLET_URL + "/json_rpc", timeout=5, json={
                "jsonrpc": "2.0",
                "id": "0",
                "method": "transfer",
                "params": {
                    "destinations": [{"amount": lokisnbot.config.TESTNET_FAUCET_AMOUNT, "address": wallet}],
                    "priority": 1,
                    }
                }).json()
        except Exception as e:
            print("testnet wallet error: {}".format(e))
            return send_reply(update, context, dead_end=True, message='💩 An error occured while communicating with the testnet wallet; please try again later')

        if 'error' in transfer and transfer['error']:
            return send_reply(update, context, dead_end=True, message='☣ *Transfer failed*: {}'.format(transfer['error']['message']))

        tx_hash = transfer['result']['tx_hash']
        cur = pgsql.cursor()
        cur.execute("UPDATE users SET faucet_last_used = %s WHERE id = %s", (int(time.time()), uid))
        send_reply(update, context, dead_end=True, message='💸 Sent you {:.9f} testnet LOKI in [{}...](https://lokitestnet.com/tx/{})'.format(
            lokisnbot.config.TESTNET_FAUCET_AMOUNT/COIN, tx_hash[0:8], tx_hash))

    else:
        send_reply(update, context,
                '{} does not look like a valid LOKI testnet wallet address!  Please check the address and send it again:'.format(wallet))


@run_async
def service_nodes_menu(update: Update, context: CallbackContext, reply_text=''):
    buttons = []
    cur = pgsql.dict_cursor()
    uid = get_uid(update, context)
    cur.execute("SELECT * FROM service_nodes WHERE uid = %s ORDER BY testnet, alias, pubkey", (uid,))
    for sn in ServiceNode.all(uid, sortkey=lambda sn: (sn['testnet'], sn['alias'] is None, sn['alias'] or sn['pubkey'])):
        snbutton = InlineKeyboardButton(
                sn.status_icon() + ' ' + ('{} ({})'.format(sn.alias(), sn.shortpub()) if sn['alias'] else sn.shortpub()),
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
    uid = get_uid(update, context)
    sns = ServiceNode.all(uid, sortkey=lambda sn: (sn['testnet'], sn.expiry_block() or float("inf"), sn['alias'] or sn['pubkey']))

    height = lokisnbot.network_info['height']
    msg = '*Service node expirations:*\n'
    testnet = False
    for sn in sns:
        if not testnet and sn['testnet']:
            msg += '\n*Testnet service node expirations:*\n'
            height = lokisnbot.testnet_network_info['height']
            testnet = True

        msg += '{} {}: '.format(sn.status_icon(), sn.alias())
        if sn.active():
            msg += 'Block _{}_ (_{}_)\n'.format(sn.expiry_block(), friendly_time(sn.expires_in()))
        else:
            msg += 'Expired/deregistered\n'

    service_nodes_menu(update, context, reply_text=msg)


@run_async
def service_node_add(update: Update, context: CallbackContext):
    context.user_data['want_add_sn'] = True
    send_reply(update, context, 'Okay, send me the public key of the service node to add:', None)


@run_async
def service_node_menu(update: Update, context: CallbackContext, ):
    snid = int(update.callback_query.data.split(':', 1)[1])
    return service_node(update, context, snid)


@run_async
def service_node_menu_inplace(update: Update, context: CallbackContext):
    snid = update.callback_query.data.split(':', 1)[1]
    sn = None
    if snid == 'last':
        snid = None
        sn = ServiceNode({ 'pubkey': context.user_data['sn_last_viewed'] })
        del context.user_data['sn_last_viewed']
    return service_node(update, context, snid=snid, sn=sn,
            callback=lambda msg, reply_markup: context.bot.edit_message_text(
                text=msg, parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup, chat_id=update.callback_query.message.chat_id,
                message_id=update.callback_query.message.message_id))


@run_async
def service_node_input(update: Update, context: CallbackContext, pubkey=None):

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
            send_reply(update, context, 'That doesn\'t look like a valid primary wallet address.  Send me at least the first 7 characters of your primary wallet address:')
            return
        del user_data['want_wallet']
        pgsql.cursor().execute("INSERT INTO wallet_prefixes (uid, wallet) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (uid, wallet))
        return wallets_menu(update, context, 'Added {}wallet _{}_.  I\'ll now calculate your share of shared contribution service node rewards.'.format(
            '*testnet* ' if wallet[0] == 'T' else '', wallet))

    elif 'want_faucet_address' in user_data:
        return turn_faucet(update, context)


    if not pubkey:
        pubkey = update.message.text

    if not re.match('^[0-9a-f]{64}$', pubkey):
        if 'want_add_sn' in user_data:
            return send_reply(update, context, message="That doesn't look like a valid service node public key; please check the key and try again:")
        else:
            return send_reply(update, context, dead_end=True, message='Sorry, I didn\'t understand your message.')

    just_looking = True
    if 'want_add_sn' in user_data:
        del user_data['want_add_sn']
        just_looking = False

    sn = None
    try:
        sn = ServiceNode(uid=uid, pubkey=pubkey)
    except ValueError:
        pass

    reply_text = ''
    if sn and not just_looking:
        reply_text = 'I am _already_ monitoring service node _{sn[pubkey]}_ for you.  Current status:'

    if not sn and just_looking:
        sn = ServiceNode({ 'pubkey': pubkey })

    elif not sn:
        sn_data = { 'pubkey': pubkey, 'uid': uid }
        sns, tsns = lokisnbot.sn_states, lokisnbot.testnet_sn_states
        if pubkey in sns:
            sn_data['active'] = True
            sn_data['complete'] = sns[pubkey]['total_contributed'] >= sns[pubkey]['staking_requirement']
            sn_data['last_reward_block_height'] = sns[pubkey]['last_reward_block_height']
            reply_text = "Okay, I'm now monitoring service node _{sn[pubkey]}_ for you.  Current status:"
        elif pubkey in tsns:
            sn_data['active'] = True
            sn_data['testnet'] = True
            sn_data['complete'] = tsns[pubkey]['total_contributed'] >= tsns[pubkey]['staking_requirement']
            sn_data['last_reward_block_height'] = tsns[pubkey]['last_reward_block_height']
            reply_text = "Okay, I'm now monitoring *testnet* service node _{sn[pubkey]}_ for you.  Current status:"
        else:
            reply_text = "Service node _{sn[pubkey]}_ isn't currently registered on the network, but I'll start monitoring it for you once it appears."

        sn = ServiceNode(sn_data)
        sn.insert()

    return service_node(update, context, sn=sn, reply_text=reply_text)


def service_node(update: Update, context: CallbackContext, snid=None, reply_text='', callback=None, pubkey=None, sn=None):
    uid = get_uid(update, context)
    if snid or pubkey:
        sn = ServiceNode(uid=uid, snid=snid, pubkey=pubkey)
    elif not sn:
        raise RuntimeError("service_node requires either snid or sn")

    if 'id' in sn:
        snid = sn['id']

    reply_text = reply_text.format(sn=sn, alias=sn.alias())
    if reply_text:
        reply_text += '\n\n'
    else:
        reply_text = 'Current status of service node _{}_:\n\n'.format(sn.alias())

    pubkey = sn['pubkey']

    sns = lokisnbot.sn_states
    if sn.testnet:
        sns = lokisnbot.testnet_sn_states
        reply_text += '🚧 This is a *testnet* service node! 🚧\n'

    if 'note' in sn and sn['note']:
        reply_text += 'Note: ' + escape_markdown(sn['note']) + '\n'

    if sn.active():
        height = (lokisnbot.testnet_network_info if sn.testnet else lokisnbot.network_info)['height']

        reply_text += 'Public key: _{}_\n'.format(sn['pubkey'])

        reply_text += 'Last uptime proof: ' + sn.format_proof_age() + '\n'

        reg_expiry = 'Block *{}* (approx. {})\n'.format(sn.expiry_block(), friendly_time(sn.expires_in()))

        cur2 = pgsql.cursor()
        cur2.execute("SELECT wallet FROM wallet_prefixes WHERE uid = %s", (uid,))
        my_wallets = [r[0] for r in cur2]
        total = sn.state('staking_requirement')
        stakes = ''.join(
                '{} stake: *{:.3f}* (_{:.1f}%_) – _{}...{}_{}\n'.format(
                    'Operator' if i == 0 else 'Contr. {}'.format(i),
                    x['amount']/COIN, x['amount']/total*100,
                    x['address'][0:7], x['address'][-2:],
                    ' 👈 (you)' if any(x['address'].startswith(w) for w in my_wallets) else '')
                for i, x in enumerate(sn.state('contributors')))

        if sn.staked():
            reply_text += 'Status: *active*\nStake: *{:.9f}*\n'.format(total/COIN)
            reply_text += stakes
            if len(sn.state('contributors')) > 1:
                reply_text += 'Operator fee: *{:.1f}%*\n'.format(sn.operator_fee() * 100)
            reply_text += 'Registration expiry: ' + reg_expiry
            if sn.state('last_reward_block_height') > sn.state('registration_height'):
                reply_text += 'Last reward at height *{}* (approx. {})\n'.format(sn.state('last_reward_block_height'), ago(
                    AVERAGE_BLOCK_SECONDS * (height - sn.state('last_reward_block_height'))))
            else:
                reply_text += 'Last reward: *never*.\n'

            lrbh = sn.state('last_reward_block_height')
            # FIXME: this is slightly wrong because it doesn't account for other SNs that may expire
            # before they earn a reward:
            blocks_to_go = 1 + sum(1 for sni in sns.values() if
                    sni['total_contributed'] >= sni['staking_requirement'] and sni['last_reward_block_height'] < lrbh)

            reply_text += 'Next reward in *{}* blocks (approx. {})\n'.format(blocks_to_go, friendly_time(blocks_to_go * AVERAGE_BLOCK_SECONDS))
        else:
            reply_text += 'Status: *awaiting contributions*\n'
            contr, req = sn.state('total_contributed'), sn.state('staking_requirement')
            reply_text += 'Stake: _{:.9f}_ (_{:.1f}%_ of required _{:.9f}_; additional contribution required: {:.9f})\n'.format(
                    contr/COIN, contr/req * 100, req/COIN, (req - contr)/COIN)
            reply_text += stakes
            reply_text += 'Operator fee: *{:.1f}%*\n'.format(sn.operator_fee() * 100)
            reply_text += 'Registration expiry: ' + reg_expiry

        if 'id' in sn:
            reply_text += 'Reward notifications: *' + ('en' if sn['rewards'] else 'dis') + 'abled*\n'
            reply_text += 'Close-to-expiry notifications: *' + ('en' if sn['expires_soon'] else 'dis') + 'abled*\n'

    else:  # not active:
        if 'alias' in sn and sn['alias']:
            reply_text += 'Service node _{}_ is not registered\n'.format(pubkey)
        else:
            reply_text += 'Not registered\n'

    expurl = explorer(sn.testnet)
    if 'id' not in sn:  # If it has no row id then it isn't something this user is watching yet
        context.user_data['sn_last_viewed'] = pubkey
        menu = InlineKeyboardMarkup([
            [InlineKeyboardButton('Refresh', callback_data='refresh:last'),
             InlineKeyboardButton('View on '+expurl, url='https://{}/service_node/{}'.format(expurl, pubkey))],
            [InlineKeyboardButton('Start monitoring {}'.format(sn.alias()), callback_data='start:last')],
            [InlineKeyboardButton('< Service nodes', callback_data='sns'), InlineKeyboardButton('<< Main menu', callback_data='main')]
            ])
    else:
        menu = InlineKeyboardMarkup([
            [InlineKeyboardButton('Refresh', callback_data='refresh:{}'.format(snid)),
             InlineKeyboardButton('View on '+expurl, url='https://{}/service_node/{}'.format(expurl, pubkey))],
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
def start_monitoring(update: Update, context: CallbackContext):
    context.user_data['want_add_sn'] = True
    pubkey = context.user_data['sn_last_viewed']
    del context.user_data['sn_last_viewed']
    service_node_input(update, context, pubkey=pubkey)


@run_async
def stop_monitoring(update: Update, context: CallbackContext):
    uid = get_uid(update, context)
    snid = int(update.callback_query.data.split(':', 1)[1])
    try:
        sn = ServiceNode(snid=snid, uid=uid)
    except ValueError:
        return service_nodes_menu(update, context, "I couldn't find that service node; please try again")
    sn.delete()
    msg = "Okay, I'm not longer monitoring service node " + (
            "_{}_ (_{}_)".format(sn['alias'], sn['pubkey']) if sn['alias'] else "_{}_".format(sn['pubkey'])) + " for you."
    return service_nodes_menu(update, context, msg)


def request_sn_field(update: Update, context: CallbackContext, field, send_fmt, current_fmt):
    uid = get_uid(update, context)
    snid = int(update.callback_query.data.split(':', 1)[1])
    try:
        sn = ServiceNode(snid=snid, uid=uid)
    except ValueError:
        send_reply(update, context, dead_end=True, message="I couldn't find that service node!")

    context.user_data['want_'+field] = snid
    msg = send_fmt.format(sn=sn, alias=sn.alias())
    if sn[field]:
        msg += '\n\n' + current_fmt.format(sn[field], escaped=escape_markdown(sn[field]))
    send_reply(update, context, msg)


def set_sn_field(update: Update, context: CallbackContext, field, value, success):
    uid = get_uid(update, context)
    snid = int(update.callback_query.data.split(':', 1)[1])
    try:
        sn = ServiceNode(snid=snid, uid=uid)
    except ValueError:
        return service_node(update, context, snid, "I couldn't find that service node!")

    sn.update(**{field: value})

    service_node(update, context, sn=sn, reply_text=success.format(sn.alias()))


@run_async
def ask_note(update: Update, context: CallbackContext):
    request_sn_field(update, context, 'note',
            "Send me a custom note to set for service node _{alias}_.",
            "The current note is: {escaped}")


@run_async
def del_note(update: Update, context: CallbackContext):
    set_sn_field(update, context, 'note', None, 'Removed note for service node _{}_.')


@run_async
def ask_alias(update: Update, context: CallbackContext):
    request_sn_field(update, context, 'alias',
            "Send me an alias to use for this service node instead of the public key (_{sn[pubkey]}_).",
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
    reply_text += 'If you send me your wallet address(es) I can calculate your specific reward and your portion of the stake (for shared contribution service nodes).'

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
    msg = 'You can either send the whole wallet address or, if you prefer, just the first 7 (or more) characters of the wallet address.  To register a wallet, send it to me now.  Use /start to cancel.'
    send_reply(update, context, msg)


def error(update: Update, context: CallbackContext):
    """Log Errors caused by Updates."""
    lokisnbot.logger.warning('Update "%s" caused error "%s"', update, context.error)


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
    elif q == 'testnet_status':
        call = testnet_status
    elif q == 'testnet_faucet':
        call = testnet_faucet
    elif q == 'add_sn':
        call = service_node_add
    elif re.match(r'sn:\d+', q):
        call = service_node_menu
    elif re.match(r'refresh:(?:\d+|last)', q):
#        bot.delete_message(chat_id=update.callback_query.message.chat_id, message_id=update.callback_query.message.message_id)
        edit = False
        call = service_node_menu_inplace
    elif q == 'start:last':
        call = start_monitoring
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


def start_bot(**kwargs):
    global updater

    updater = Updater(lokisnbot.config.TELEGRAM_TOKEN, workers=4, use_context=True, **kwargs)

    # Get the dispatcher to register handlers
    dp = updater.dispatcher

    updater.dispatcher.add_handler(CommandHandler('start', start))
    updater.dispatcher.add_handler(CallbackQueryHandler(dispatch_query))
    updater.dispatcher.add_handler(MessageHandler(Filters.text, service_node_input))

    # log all errors
    dp.add_error_handler(error)

    # Start the Bot
    updater.start_polling()
