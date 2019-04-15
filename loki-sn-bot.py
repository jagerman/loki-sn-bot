#!/usr/bin/python3

import threading
import time
import requests
import traceback
import signal
import asyncio

import loki_sn_bot_config as config

import lokisnbot
lokisnbot.config = config
from lokisnbot.constants import *
import lokisnbot.util as util
from lokisnbot.telegram import TelegramNetwork
from lokisnbot.discord import DiscordNetwork
import lokisnbot.pgsql as pgsql
from lokisnbot.servicenode import ServiceNode, reward
#import lokisnbot.discord as dc

if not hasattr(config, 'WELCOME'):
    config.WELCOME = (
            'Hi!  I can give you Loki service node information and send you alerts if the uptime proof for your service node(s) gets too long.  ' +
            'I can also optionally let you know when your service nodes earn a payment and when your service node is nearing expiry.' +
            ('\n\nI am also capable of monitoring testnet service nodes if you send me a pubkey for a service node on the testnet.' if config.TESTNET_NODE_URL else '') +
            ('\n\nI also have a testnet wallet attached: if you need some testnet funds use the testnet faucet menu to have me send some testnet LOKI your way.' if config.TESTNET_WALLET_URL and config.TESTNET_FAUCET_AMOUNT else '') +
            ('\n\nThis bot is operated by {owner}' if config.TELEGRAM_OWNER or config.DISCORD_OWNER else '') +
            ('\n\n' + config.EXTRA if config.EXTRA else '')
            )




tg, dc = None, None



def notify(sn, msg, is_update=True):
    """Notify based on Telegram/Discord status.  Returns true if at least one notification went out.
    is_update controls whether this is a status update, in which case extra info (a link to
    lokiblocks) is added; True by default, should be false for boring notifications like rewards"""
    global tg, dc

    tgid, dcid = sn['telegram_id'], sn['discord_id']
    good = 0
    if tgid:
        extra = tg.sn_update_extra(sn) if is_update else {}
        if tg.try_message(tgid, msg, **extra):
            good += 1
    if dcid:
        extra = dc.sn_update_extra(sn) if is_update else {}
        if dc.try_message(dcid, msg, **extra):
            good += 1

    return good > 0


time_to_die = False
def loki_updater():
    global time_to_die, tg, dc
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

        if not tg or not tg.ready() or not dc or not dc.ready:
            print("bots not ready yet!")
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
                if not sn['telegram_id'] and not sn['discord_id']:
                    continue
                if sn.testnet and not tsns:
                    continue  # Ignore: testnet node didn't respond
                uid = sn['uid']
                chatid = sn['telegram_id']
                pubkey = sn['pubkey']
                name = sn.alias()
                netheight = testnet_height if sn.testnet else mainnet_height

                if not sn.active():
                    if not sn['notified_dereg']:
                        dereg_msg = ('📅 Service node _{}_ reached the end of its registration period and is no longer registered on the network.'.format(name)
                                if pubkey in expected_dereg_height and 0 < expected_dereg_height[pubkey] <= netheight else
                                '🛑 *UNEXPECTED DEREGISTRATION!* Service node _{}_ is no longer registered on the network! 😦'.format(name))
                        if notify(sn, dereg_msg):
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
                            if notify(sn, prefix+'⚠ *WARNING:* Service node _{}_ last uptime proof is *{}*'.format(name, sn.format_proof_age())):
                                sn.update(notified_age=proof_age)
                    elif sn['notified_age']:
                        if notify(sn, prefix+'😌 Service node _{}_ last uptime proof received (now *{}*)'.format(name, sn.format_proof_age())):
                            sn.update(notified_age=None)

                just_completed = False
                if not sn['complete']:
                    if not sn['last_contributions'] or sn['last_contributions'] < sn.state('total_contributed'):
                        pct = sn.state('total_contributed') / sn.state('staking_requirement') * 100
                        msg_part_a = ('{} Service node _{}_ is awaiting contributions.' if not sn['last_contributions'] else
                                '{} Service node _{}_ received a contribution.').format(sn.moon_symbol(pct), name)

                        if notify(sn, prefix + msg_part_a + '  Total contributions: _{:.9f}_ (_{:.1f}%_ of required _{:.9f}_).  Additional contribution required: _{:.9f}_.'.format(
                                sn.state('total_contributed')*1e-9, pct, sn.state('staking_requirement')*1e-9, (sn.state('staking_requirement') - sn.state('total_contributed'))*1e-9)):
                            sn.update(last_contributions=sn.state('total_contributed'))

                    if sn.state('total_contributed') >= sn.state('staking_requirement'):
                        if notify(sn, prefix+'💚 Service node _{}_ is now fully staked and active!'.format(name)):
                            sn.update(complete=True)
                        just_completed = True


                req_unlock = None
                if sn.infinite_stake():
                    req_height = sn.expiry_block()
                    if req_height is None:
                        if sn['requested_unlock_height'] is not None or sn['unlock_notified']:
                            sn.update(requested_unlock_height=None, unlock_notified=False)
                    elif not sn['unlock_notified']:
                        if notify(sn, prefix+'📆 💔 Service node _{}_ has started a stake unlock.  Stakes will unlock in {} (at block _{}_)'.format(
                                name, util.friendly_time((req_height - netheight) * AVERAGE_BLOCK_SECONDS), req_height)):
                            sn.update(unlock_notified=True, requested_unlock_height=req_height)


                snver = sn.version()
                if snver:
                    if config.WARN_VERSION_LESS_THAN and snver < config.WARN_VERSION_LESS_THAN:
                        if not sn['notified_obsolete'] or sn['notified_obsolete'] + 12*60*60 <= now:
                            if notify(sn, prefix+'⚠ *WARNING:* Service node _{}_ is running *v{}*\n{}\nIf not upgraded before the fork this service node will deregister!'.format(
                                    name, sn.version_str(), config.WARN_VERSION_MSG)):
                                sn.update(notified_obsolete=now)
                    elif sn['notified_obsolete']:
                        if notify(sn, prefix+'💖 Service node _{}_ is now running *v{}*.  Thanks for upgrading!'.format(name, snnver)):
                            sn.update(notified_obsolete=None)

                    update_lv = False
                    if sn['last_version']:
                        msg = None
                        if snver > sn['last_version'] > [0, 0, 0]:
                            msg = prefix+'💖 Service node _{}_ upgraded to *v{}* (from *v{}*)'
                        elif [0, 0, 0] < snver < sn['last_version']:
                            msg = prefix+'💔 Service node _{}_ *downgraded* to *v{}* (from *v{}*)!'

                        if msg and notify(sn, msg.format(name, ServiceNode.to_version_string(snver), ServiceNode.to_version_string(sn['last_version']))):
                            update_lv = True
                    else:
                        update_lv = True

                    if update_lv:
                        sn.update(last_version=snver)


#                if (snver and snver in ("3.0.0", "3.0.1")) or (snver is None and not sn['notified_obsolete']):
#                    if not sn['notified_v300'] or sn['notified_v300'] + 60*60 <= now:
#                        if notify(sn,
#                                '🛑🛑🛑 *WARNING*  Service node _{}_ is running *v3.0.0* or *v3.0.1*, but a critical bug was discovered that will result in deregistrations.  An emergency update '
#                                'to *v3.0.2* is ready for immediate deployment.  Note that the previous *v3.0.1* release did *NOT* fully correct the problem. '
#                                'Please go to the Loki Service Nodes group (https://t.me/LokiServiceNodes) for more details.  (And apologies for the notifications, but this was important!)'.format(name)):
#                            sn.update(notified_v300=now)


                if sn['expires_soon']:
                    expires_at, expires_in = sn.expiry_block(), sn.expires_in()
                    if sn.infinite_stake() and expires_at is None:
                        if sn['expiry_notified']:
                            sn.update(expiry_notified=None)
                    else:
                        notify_time = next((int(t*3600) for t in (config.TESTNET_EXPIRY_THRESHOLDS if sn.testnet else config.EXPIRY_THRESHOLDS) if expires_in <= t*3600), None)
                        if notify_time and (not sn['expiry_notified'] or sn['expiry_notified'] > notify_time):
                            hformat = '{:.0f}' if expires_in >= 7200 else '{:.1f}'
                            if notify(sn, prefix+('⏱ Service node _{}_ registration expires in about '+hformat+' hour{} (block _{}_)').format(
                                    name, expires_in/3600, '' if expires_in == 3600 else 's', expires_at)):
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

                        if notify(sn, prefix+'💰 Service node _{}_ earned a reward of *{:.3f} LOKI* at height *{}*.'.format(name, snreward, lrbh) + (
                                    '  Your share: ' + ', '.join(my_rewards) if my_rewards else ''), is_update=False):
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


def stop_threads(signum, frame):
    print("Stopping threads and shutting down...")
    global time_to_die, loki_thread
    time_to_die = True
    loki_thread.join()
    print("Stopped updater thread")

    global tg, dc
    tg.stop()
    print("Stopped Telegram")

    print("Stopping Discord")
    dc.stop()

def main():
    pgsql.connect()

    start_loki_update_thread()

    print("Starting Telegram bot")

    global tg, dc
    tg = TelegramNetwork()
    dc = DiscordNetwork()

    tg.start()
    dc.start()

    signal.signal(signal.SIGINT, stop_threads)
    signal.signal(signal.SIGTERM, stop_threads)
    signal.signal(signal.SIGABRT, stop_threads)

    print("Bot started")

    pending = asyncio.Task.all_tasks()
    asyncio.get_event_loop().run_until_complete(asyncio.gather(*pending))

    print("Bot ended")


if __name__ == '__main__':
    main()
