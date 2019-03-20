#!/usr/bin/python3

import threading
import time
import requests
import traceback

import loki_sn_bot_config as config

import lokisnbot
lokisnbot.config = config
from lokisnbot.constants import *
import lokisnbot.util as util
import lokisnbot.telegram as tg
import lokisnbot.pgsql as pgsql
from lokisnbot.servicenode import ServiceNode, reward
#import lokisnbot.discord as dc

if not hasattr(config, 'WELCOME'):
    config.WELCOME = (
            'Hi!  I can give you loki service node information and send you alerts if the uptime proof for your service node(s) gets too long.  ' +
            'I can also optionally let you know when your service nodes earn a payment and when your service node is nearing expiry.' +
            ('\n\nI am also capable of monitoring testnet service nodes if you send me a pubkey for a service node on the testnet.' if config.TESTNET_NODE_URL else '') +
            ('\n\nI also have a testnet wallet attached: if you need some testnet funds use the testnet faucet menu to have me send some testnet LOKI your way.' if config.TESTNET_WALLET_URL and config.TESTNET_FAUCET_AMOUNT else '') +
            ('\n\nThis bot is operated by ' + config.OWNER if config.OWNER else '') +
            ('\n\n' + config.EXTRA if config.EXTRA else '')
            )








time_to_die = False
def loki_updater():
    global time_to_die
    expected_dereg_height = {}
    last = 0
    while not time_to_die:
        now = time.time()
        if now - last < 10:
            time.sleep(0.25)
            continue

        try:
            status = requests.get(config.NODE_URL + '/get_info', timeout=2).json()
            sns = requests.post(config.NODE_URL + '/json_rpc', json={"jsonrpc":"2.0","id":"0","method":"get_service_nodes"},
                    timeout=2).json()['result']['service_node_states']
        except Exception as e:
            print("An exception occured during loki stats fetching: {}".format(e))
            continue
        last = now
        sns = { x['service_node_pubkey']: x for x in sns }
        lokisnbot.sn_states, lokisnbot.network_info = sns, status

        tsns, tstatus = None, None
        if config.TESTNET_NODE_URL:
            try:
                tstatus = requests.get(config.TESTNET_NODE_URL + '/get_info', timeout=2).json()
                tsns = requests.post(config.TESTNET_NODE_URL + '/json_rpc', json={"jsonrpc":"2.0","id":"0","method":"get_service_nodes"},
                        timeout=2).json()['result']['service_node_states']
                tsns = { x['service_node_pubkey']: x for x in tsns }
                lokisnbot.testnet_sn_states, lokisnbot.testnet_network_info = tsns, tstatus
            except Exception as e:
                print("An exception occured during loki testnet stats fetching: {}; ignoring the error".format(e))
                tsns, tstatus = None, None

        for s, infinite_from, finite_blocks in (
                (tsns, TESTNET_INFINITE_FROM, TESTNET_STAKE_BLOCKS),
                (sns, INFINITE_FROM, STAKE_BLOCKS)):
            if not s:
                continue
            for pubkey, x in s.items():
                if x['registration_height'] >= infinite_from:
                    expected_dereg_height[pubkey] = x['requested_unlock_height']
                else:
                    expected_dereg_height[pubkey] = x['registration_height'] + TESTNET_STAKE_BLOCKS

        if not hasattr(tg.updater, 'bot'):
            print("no bot yet!")
            continue
        try:
            cur = pgsql.dict_cursor()
            wallets = {}
            cur.execute("SELECT uid, wallet FROM wallet_prefixes")
            for row in cur:
                if row[0] not in wallets:
                    wallets[row[0]] = []
                wallets[row[0]].append(row[1])

            mainnet_height = status['height']
            testnet_height = tstatus['height'] if tsns else None

            inactive = []
            cur.execute("SELECT users.telegram_id, users.discord_id, service_nodes.* FROM users JOIN service_nodes ON uid = users.id ORDER BY uid")
            for row in cur:
                sn = ServiceNode(row)
                if not sn['telegram_id']:
                    continue  # FIXME
                if sn.testnet and not tsns:
                    continue  # Ignore: testnet node didn't respond
                uid = sn['uid']
                chatid = sn['telegram_id']
                pubkey = sn['pubkey']
                name = sn.alias()
                netheight = testnet_height if sn.testnet else mainnet_height
                explorer = util.explorer(testnet=sn.testnet)

                sn_details_buttons = tg.InlineKeyboardMarkup([[
                    tg.InlineKeyboardButton('SN details', callback_data='sn:{}'.format(sn['id'])),
                    tg.InlineKeyboardButton(explorer, url='https://{}/service_node/{}'.format(explorer, pubkey))]])

                if not sn.active():
                    if not sn['notified_dereg']:
                        dereg_msg = ('📅 Service node _{}_ reached the end of its registration period and is no longer registered on the network.'.format(name)
                                if pubkey in expected_dereg_height and 0 < expected_dereg_height[pubkey] <= netheight else
                                '🛑 *UNEXPECTED DEREGISTRATION!* Service node _{}_ is no longer registered on the network! 😦'.format(name))
                        if tg.send_message_or_shutup(tg.updater.bot, chatid, dereg_msg, reply_markup=sn_details_buttons):
                            sn.update(active=False, notified_dereg=True, complete=False, last_contributions=0, expiry_notified=None)
                    elif sn['active']:
                        sn.update(active=False)

                    continue
                elif sn['notified_dereg'] or not sn['active']:
                    sn.update(active=True, notified_dereg=False)

                prefix = '🚧' if sn.testnet else ''

                proof_age = sn.proof_age()
                if proof_age is not None:
                    if proof_age >= PROOF_AGE_WARNING:
                        if not sn['notified_age'] or proof_age - sn['notified_age'] > PROOF_AGE_REPEAT:
                            if tg.send_message_or_shutup(tg.updater.bot, chatid,
                                    prefix+'⚠ *WARNING:* Service node _{}_ last uptime proof is *{}*'.format(name, sn.format_proof_age()),
                                    reply_markup=sn_details_buttons):
                                sn.update(notified_age=proof_age)
                    elif sn['notified_age']:
                        if tg.send_message_or_shutup(tg.updater.bot, chatid,
                                prefix+'😌 Service node _{}_ last uptime proof received (now *{}*)'.format(name, sn.format_proof_age()),
                                reply_markup=sn_details_buttons):
                            sn.update(notified_age=None)

                just_completed = False
                if not sn['complete']:
                    if not sn['last_contributions'] or sn['last_contributions'] < sn.state('total_contributed'):
                        pct = sn.state('total_contributed') / sn.state('staking_requirement') * 100
                        msg_part_a = ('{} Service node _{}_ is awaiting contributions.' if not sn['last_contributions'] else
                                '{} Service node _{}_ received a contribution.').format(sn.moon_symbol(pct), name)

                        if tg.send_message_or_shutup(tg.updater.bot, chatid,
                                prefix + msg_part_a + '  Total contributions: _{:.9f}_ (_{:.1f}%_ of required _{:.9f}_).  Additional contribution required: _{:.9f}_.'.format(
                                    sn.state('total_contributed')*1e-9, pct, sn.state('staking_requirement')*1e-9, (sn.state('staking_requirement') - sn.state('total_contributed'))*1e-9),
                                reply_markup=sn_details_buttons):
                            sn.update(last_contributions=sn.state('total_contributed'))

                    if sn.state('total_contributed') >= sn.state('staking_requirement'):
                        if tg.send_message_or_shutup(tg.updater.bot, chatid,
                                prefix+'💚 Service node _{}_ is now fully staked and active!'.format(name),
                                reply_markup=sn_details_buttons):
                            sn.update(complete=True)
                        just_completed = True


                req_unlock = None
                if sn.infinite_stake():
                    req_height = sn.expiry_block()
                    if req_height is None:
                        if sn['requested_unlock_height'] is not None or sn['unlock_notified']:
                            sn.update(requested_unlock_height=None, unlock_notified=False)
                    elif not sn['unlock_notified']:
                        if tg.send_message_or_shutup(tg.updater.bot, chatid,
                                '📆 💔 Service node _{}_ has started a stake unlock.  Stakes will unlock in {} (at block _{}_)'.format(
                                    name, util.friendly_time((req_height - netheight) * AVERAGE_BLOCK_SECONDS), req_height),
                                reply_markup=sn_details_buttons):
                            sn.update(unlock_notified=True, requested_unlock_height=req_height)


                snver = sn.version()
                if snver and config.WARN_VERSION_LESS_THAN and snver < config.WARN_VERSION_LESS_THAN:
                    if not sn['notified_obsolete'] or sn['notified_obsolete'] + 12*60*60 <= now:
                        if tg.send_message_or_shutup(tg.updater.bot, chatid,
                                prefix+'⚠ *WARNING:* Service node _{}_ is running *v{}*\n{}\nIf not upgraded before the fork this service node will deregister!'.format(name, sn.version(), config.WARN_VERSION_MSG),
                                reply_markup=sn_details_buttons):
                            sn.update(notified_obsolete=now)
                elif snver and sn['notified_obsolete']:
                    if tg.send_message_or_shutup(tg.updater.bot, chatid,
                            prefix+'💖 Service node _{}_ is now running *v{}*.  Thanks for upgrading!'.format(name, sn.version()),
                            reply_markup=sn_details_buttons):
                        sn.update(notified_obsolete=None)


                if sn['expires_soon']:
                    expires_at, expires_in = sn.expiry_block(), sn.expires_in()
                    if sn.infinite_stake() and expires_at is None:
                        if sn['expiry_notified']:
                            sn.update(expiry_notified=None)
                    else:
                        notify_time = next((int(t*3600) for t in (config.TESTNET_EXPIRY_THRESHOLDS if sn.testnet else config.EXPIRY_THRESHOLDS) if expires_in <= t*3600), None)
                        if notify_time and (not sn['expiry_notified'] or sn['expiry_notified'] > notify_time):
                            hformat = '{:.0f}' if expires_in >= 7200 else '{:.1f}'
                            if tg.send_message_or_shutup(tg.updater.bot, chatid,
                                    prefix+('⏱ Service node _{}_ registration expires in about '+hformat+' hour{} (block _{}_)').format(
                                        name, expires_in/3600, '' if expires_in == 3600 else 's', expires_at),
                                    reply_markup=sn_details_buttons):
                                sn.update(expiry_notified=notify_time)
                        elif notify_time is None and sn['expiry_notified']:
                            sn.update(expiry_notified=None)

                lrbh = sn.state('last_reward_block_height')
                if not sn['last_reward_block_height']:
                    sn.update(last_reward_block_height=lrbh)
                elif sn['last_reward_block_height'] and lrbh > sn['last_reward_block_height']:
                    if sn['rewards'] and not just_completed and sn.state('total_contributed') >= sn.state('staking_requirement'):
                        snreward = reward(lrbh)
                        my_rewards = []
                        if sn['uid'] in wallets and len(sn.state('contributors')) > 1:
                            for y in sn.state('contributors'):
                                if any(y['address'].startswith(x) for x in wallets[sn['uid']]):
                                    operator_reward = snreward * sn.operator_fee()
                                    mine = (snreward - operator_reward) * y['amount'] / sn.state('staking_requirement')
                                    if y['address'] == sn.state('operator_address'):
                                        mine += operator_reward
                                    my_rewards.append('*{:.3f} LOKI* (_{}...{}_)'.format(mine, y['address'][0:7], y['address'][-3:]))

                        if tg.send_message_or_shutup(tg.updater.bot, chatid,
                                prefix+'💰 Service node _{}_ earned a reward of *{:.3f} LOKI* at height *{}*.'.format(name, snreward, lrbh) + (
                                    '  Your share: ' + ', '.join(my_rewards) if my_rewards else '')):
                            sn.update(last_reward_block_height=lrbh)
                    else:
                        sn.update(last_reward_block_height=lrbh)


        except Exception as e:
            print("An exception occured during updating/notifications: {}".format(e))
            import sys
            traceback.print_exc(file=sys.stdout)
            continue


loki_thread = None
def start_loki_update_thread():
    global loki_thread
    loki_thread = threading.Thread(target=loki_updater)
    loki_thread.start()
    while True:
        if lokisnbot.network_info:
            print("Loki data fetched")
            return
        time.sleep(0.25)


def stop_loki_thread(signum, frame):
    global time_to_die, loki_thread
    time_to_die = True
    loki_thread.join()


def main():
    pgsql.connect()

    start_loki_update_thread()

    print("Starting Telegram bot")

    tg.start_bot(user_sig_handler=stop_loki_thread)

    print("Bot started")

    # Run the bot until you press Ctrl-C or the process receives SIGINT,
    # SIGTERM or SIGABRT. This should be used most of the time, since
    # start_polling() is non-blocking and will stop the bot gracefully.
    tg.updater.idle()


if __name__ == '__main__':
    main()
