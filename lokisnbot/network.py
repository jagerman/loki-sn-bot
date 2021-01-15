
from abc import ABCMeta, abstractmethod
import time
import requests
import re

import lokisnbot
from . import pgsql
from .constants import *
from .util import friendly_time, ago, explorer, escape_markdown
from .servicenode import ServiceNode, lsr, reward

last_faucet_use = 0

class Network(metaclass=ABCMeta):

    @abstractmethod
    def start(self):
        """Starts a thread, sets up a webhook, etc. to start handling messages from the network"""
        pass

    @abstractmethod
    def try_message(self, chatid, message, *args, **kwargs):
        """Tries sending a message, returning True if successful, false if it failed"""
        return False

    def sn_update_extra(self, sn):
        """Returns a dict of any extra kwargs to pass to try_message when sending a message about a
        SN status update.  The default adds nothing."""
        return {}

    @abstractmethod
    def ready(self):
        """Returns true if the bot is connected and ready to go"""
        return False


class NetworkContext(metaclass=ABCMeta):

    @staticmethod
    def b(text):
        """Returns text with bold markup.  Default returns text as-is"""
        return "{}".format(text)


    @staticmethod
    def i(text):
        """Returns text with italic markup.  Default returns text as-is"""
        return "{}".format(text)


    @staticmethod
    def escape_msg(text):
        """Escapes anything in the message that needs to be escaped"""
        return "{}".format(text)


    @abstractmethod
    def send_reply(self, message: str, dead_end=False, expect_reply=True):
        """Sends a reply.
        - dead_end indicates a message that "ends" a conversation (so that, for example, a Main menu
            button can be added if supported by the network).
        - expect_reply indicates that the bot is asking the user for something, so could, for example,
            put the user in reply mode.
        """
        raise RuntimeError("base method send_reply should not be called")


    @abstractmethod
    def get_uid(self):
        raise RuntimeError("base method get_uid should not be called")


    def get_user_field(self, field):
        """Queries and returns the `field` row from the database for the current user"""
        uid = self.get_uid()
        cur = pgsql.cursor()
        cur.execute("SELECT "+field+" FROM users WHERE id = %s", (uid,))
        return cur.fetchone()[0]


    def set_user_field(self, field, value):
        """Sets the `field` row in the database to the given value.  Returns the value as set in the
        database (which could be different from the input value if conversion happened)"""
        uid = self.get_uid()
        cur = pgsql.cursor()
        cur.execute("UPDATE users SET "+field+" = %s WHERE id = %s RETURNING "+field, (value, uid))
        return cur.fetchone()[0]


    @abstractmethod
    def is_dm(self):
        pass


    def is_wallet(self, wallet, *, mainnet, testnet, primary=False, partial=False):
        """Returns true if the wallet looks like a wallet for mainnet or testnet (depending on the
        given kwargs options).  By default integrated/subaddress wallet addresses are accepted, but
        this can be disabled using the `primary=True` keyword arg.  If both primary and partial are
        given, the wallet may be just a primary wallet prefix."""
        if partial and len(wallet) < lokisnbot.config.PARTIAL_WALLET_MIN_LENGTH:
            return False
        patterns = []
        if mainnet:
            patterns += (lokisnbot.config.PARTIAL_WALLET_MAINNET if partial else lokisnbot.config.MAINNET_WALLET,) if primary else lokisnbot.config.MAINNET_WALLET_ANY
        if testnet:
            patterns += (lokisnbot.config.PARTIAL_WALLET_TESTNET if partial else lokisnbot.config.TESTNET_WALLET,) if primary else lokisnbot.config.TESTNET_WALLET_ANY
        return any(re.match(p, wallet) for p in patterns)


    def breakup_long_message(self, msg, maxlen):
        msgs = []
        while len(msg) > maxlen:
            # Discord/Telegram max message is 2000/4096 (unicode) characters; try to chop up a long message on the
            # last "space" in the last 3/4 of that (500-2000 or 1024-4096 character range).  First
            # we look for a double-newline, then if not found a single newline, then if not found a
            # space.  If still not found just hard chop at the max.
            chopped = False
            for space in ("\n\n", "\n", " "):
                pos = msg.rfind(space, maxlen//4, maxlen)
                if pos != -1:
                    chopped = True
                    msgs.append(msg[0:pos-1])
                    msg = msg[pos:]
                    break
            if not chopped:
                msgs.append(msg[0:maxlen])
                msg = msg[maxlen:]
        if len(msg) > 0:
            msgs.append(msg)
        return msgs


    def main_menu(self, reply, **kwargs):
        if reply:
            reply += '\n\n'

        uid = self.get_uid()
        cur = pgsql.cursor()
        cur.execute("SELECT COUNT(*), testnet FROM service_nodes WHERE uid = %s GROUP BY testnet", (uid,))
        mainnet, testnet = 0, 0
        for row in cur:
            if row[1]:
                testnet = row[0]
            else:
                mainnet = row[0]

        if mainnet > 0 or testnet > 0:
            reply += "I am currently monitoring {} service node{}".format(self.b(mainnet), 's' if mainnet != 1 else '')
            if testnet > 0:
                reply += " and {} testnet service node{}".format(self.b(testnet), 's' if testnet != 1 else '')
            reply += " for you."
        else:
            reply += "I am not currently monitoring any service nodes for you."

        self.send_reply(reply, **kwargs)


    def status(self, testnet=False, **kwargs):
        sns = lokisnbot.testnet_sn_states if testnet else lokisnbot.sn_states
        active, decomm, waiting, infinite, old_proof = 0, 0, 0, 0, 0
        unlocking = [0, 0, 0, 0]  # <1 d, <3 days, <1 week, >1 week
        version_counts = {}
        now = int(time.time())
        h = (lokisnbot.testnet_network_info if testnet else lokisnbot.network_info)['height']
        for sn in sns.values():
            if sn['total_contributed'] < sn['staking_requirement']:
                waiting += 1
            elif 'active' not in sn or sn['active']:
                active += 1
            else:
                decomm += 1
            if sn['registration_height'] >= (TESTNET_INFINITE_FROM if testnet else INFINITE_FROM):
                if sn['requested_unlock_height']:
                    unlock_days = (sn['requested_unlock_height'] - h) // 720
                    unlocking[0 if unlock_days < 1 else 1 if unlock_days < 3 else 2 if unlock_days < 7 else 3] += 1
                else:
                    infinite += 1
            if sn['last_uptime_proof'] and now - sn['last_uptime_proof'] > PROOF_AGE_WARNING:
                old_proof += 1
            ver = ServiceNode.to_version_string(sn['service_node_version']) if 'service_node_version' in sn else None
            if ver not in version_counts:
                version_counts[ver] = 1
            else:
                version_counts[ver] += 1

        b = lambda x: self.b(x)
        i = lambda x: self.i(x)
        reply_text = '🚧 ' + b('Testnet') + ' 🚧\n' if testnet else ''
        reply_text += 'Network height: {}\n'.format(b(h));
        reply_text += 'Service nodes: {} {} + {} {} + {} {}\n'.format(b(active), i('(active)'), b(decomm), i('(decomm.)'), b(waiting), i('(awaiting stake)'))
        if infinite or any(unlocking):
            reply_text += 'SNs unlocking: {total} ({n[0]} {u[0]}, {n[1]} {u[1]}, {n[2]} {u[2]}, {n[3]} {u[3]})\n'.format(
                    total=b(sum(unlocking)), u=[b(u) for u in unlocking], n=[i('<1d:'), i('1-3d:'), i('3-7d:'), i('≥7d:')])
        reply_text += '{} service node'.format(b(old_proof)) + (' has uptime proof' if old_proof == 1 else 's have uptime proofs') + ' > 1h5m\n';

        if len(version_counts) > (1 if None in version_counts else 0):
            reply_text += 'SN versions: ' + ', '.join(
                    ('{} '+i('[{}]')).format(b(v) if v else i("unknown"), version_counts[v])
                    for v in sorted(version_counts.keys(), key=lambda x: x or "0.0.0", reverse=True)) + '\n'

        snbr = reward(h)  # 0.5 * (28 + 100 * 2**(-h/64800))
        reply_text += 'Current SN stake requirement: {} OXEN\n'.format(b('{:.2f}'.format(lsr(h, testnet=testnet))))
        reply_text += 'Current SN reward: {} OXEN\n'.format(b('{:.4f}'.format(snbr)))

        testnet_clause = "testnet" if testnet else "NOT testnet"
        cur = pgsql.cursor()
        cur.execute("SELECT COUNT(*) FROM (SELECT DISTINCT pubkey FROM service_nodes WHERE active AND "+testnet_clause+") AS sns")
        monitored_sns = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM (SELECT DISTINCT users.id FROM users JOIN service_nodes ON uid = users.id WHERE active AND "+testnet_clause+") AS usrs")
        active_users = cur.fetchone()[0]

        reply_text += 'I am currently monitoring {} active {}service nodes ({}) on behalf of {} users.'.format(
                b(monitored_sns),
                b("testnet ") if testnet else "",
                b('{:.1f}%'.format(100 * monitored_sns / (active + waiting))),
                b(active_users))

        if self.is_dm():
            self.main_menu(reply_text, **kwargs)
        else:
            self.send_reply(reply_text, **kwargs)


    def faucet_was_recently_used(self):
        """Checks if the faucet was recently used and, if so, sends a reply to the user and returns
        True.  Otherwise sends nothing and returns True."""
        cur = pgsql.cursor()
        cur.execute("SELECT faucet_last_used FROM users WHERE id = %s", (self.get_uid(),))
        last_used = cur.fetchone()[0]
        if last_used is None:
            last_used = 0

        global last_faucet_use
        now = int(time.time())
        global_wait = (last_faucet_use - now) + lokisnbot.config.TESTNET_FAUCET_WAIT_GLOBAL
        user_wait = (last_used - now) + lokisnbot.config.TESTNET_FAUCET_WAIT_USER
        if user_wait > 0:
            self.send_reply(dead_end=True,
                    message="🤔 It appears that you have already used the faucet recently.  You need to wait another {} before you can use it again.".format(
                        friendly_time(user_wait)))
            return True
        elif global_wait > 0:
            self.send_reply(dead_end=True,
                    message="🤔 The faucet has been used by someone else recently.  You need to wait another {} before you can use it.".format(
                        friendly_time(global_wait)))
            return True
        return False

    def send_faucet_tx(self, wallet):
        """Tries to send a faucet transaction.  Upon error, sends an error message and returns None.
        Upon success, returns the `result` element of the transaction response."""
        try:
            transfer = requests.post(lokisnbot.config.TESTNET_WALLET_URL + "/json_rpc", timeout=5, json={
                "jsonrpc": "2.0",
                "id": "0",
                "method": "transfer",
                "params": {
                    "destinations": [{"amount": lokisnbot.config.TESTNET_FAUCET_AMOUNT, "address": wallet}],
                    "priority": 5,
                    }
                }).json()
        except Exception as e:
            print("testnet wallet error: {}".format(e))
            return self.send_reply(dead_end=True, message='💩 An error occured while communicating with the testnet wallet; please try again later')

        if 'error' in transfer and transfer['error']:
            print("Faucet transfer error: {}".format(transfer['error']))
            return self.send_reply(dead_end=True, message='☣ '+self.b('Transfer failed')+': {}'.format(transfer['error']['message']))

        print("Faucet success: {}".format(transfer['result']['tx_hash']))
        global last_faucet_use
        last_faucet_use = int(time.time())
        pgsql.cursor().execute("UPDATE users SET faucet_last_used = %s WHERE id = %s", (int(time.time()), self.get_uid()))

        return transfer['result']


    def service_node(self, *, snid=None, reply_text='', pubkey=None, sn=None, send=True):
        """Shows service node details.  If `send` is false, returns (msg, sn) instead of sending it"""

        uid = self.get_uid()
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
            reply_text = 'Current status of service node {}:\n\n'.format(self.i(sn.alias()))

        pubkey = sn['pubkey']

        sns = lokisnbot.sn_states
        if sn.testnet:
            sns = lokisnbot.testnet_sn_states
            reply_text += '🚧 This is a '+self.b('testnet')+' service node! 🚧\n'

        if 'note' in sn and sn['note']:
            reply_text += 'Note: ' + self.escape_msg(sn['note']) + '\n'

        if sn.active():
            height = (lokisnbot.testnet_network_info if sn.testnet else lokisnbot.network_info)['height']

            reply_text += 'Public key: {}\n'.format(self.i(pubkey))
            reply_text += 'Lokinet address: {}\n'.format(self.i(sn.lokinet_snode_addr()))

            reply_text += 'Last uptime proof: ' + sn.format_proof_age() + '\n'

            ver, verstr = sn.version(), sn.version_str()
            reply_text += 'Service node version: ' + (self.b(verstr) if verstr else 'unknown')
            if lokisnbot.config.WARN_VERSION_MSG and lokisnbot.config.WARN_VERSION_LESS_THAN and ver and ver < lokisnbot.config.WARN_VERSION_LESS_THAN:
                reply_text += ' ' + lokisnbot.config.WARN_VERSION_MSG
            reply_text += '\n'

            expiry_block = sn.expiry_block()
            if sn.infinite_stake() and expiry_block is None:
                reg_expiry = 'Never (infinite stake)\n'
            else:
                reg_expiry = 'Block {} (approx. {})\n'.format(self.b(sn.expiry_block()), friendly_time(sn.expires_in()))

            cur2 = pgsql.cursor()
            cur2.execute("SELECT wallet FROM wallet_prefixes WHERE uid = %s", (uid,))
            my_wallets = [r[0] for r in cur2]
            total = sn.state('staking_requirement')
            stakes = ''.join(
                    ('{} stake: '+self.b('{:.3f}')+' ('+self.i('{:.1f}%')+') – '+self.i('{}...{}')+'{}\n').format(
                        'Operator' if i == 0 else 'Contr. {}'.format(i),
                        x['amount']/COIN, x['amount']/total*100,
                        x['address'][0:7], x['address'][-2:],
                        ' 👈 (you)' if any(x['address'].startswith(w) for w in my_wallets) else '')
                    for i, x in enumerate(sn.state('contributors')))

            if sn.staked():
                reg_height = sn.state('registration_height')
                status = self.b('Active' if sn.active_on_network() else 'DECOMMISSIONED!')
                reply_text += 'Status: ' + sn.status_icon() + ' ' + status + '\n'

                reply_text += 'Public IP: ' + self.b(sn.state('public_ip')) + '\n'

                if sn.active_on_network() and sn.decomm_credit_blocks():
                    reply_text += 'Earned uptime credit: ' + sn.format_decomm_credit()
                    if sn.decomm_credit_blocks() < 2*3600 / AVERAGE_BLOCK_SECONDS:
                        reply_text += ' (min. 2 hours required)'
                    reply_text += '\n'
                elif sn.decommissioned():
                    reply_text += 'Remaining decommssion time: ' + sn.format_decomm_credit() + '\n'

                reply_text += ('Stake: '+self.b('{:.9f}')+'\nReg. height: '+self.b('{}')+' (approx. {})\n').format(
                        total/COIN, reg_height, ago((height - reg_height) * AVERAGE_BLOCK_SECONDS))

                reply_text += stakes
                if len(sn.state('contributors')) > 1:
                    reply_text += 'Operator fee: '+self.b('{:.1f}%'.format(sn.operator_fee() * 100))+'\n'
                reply_text += 'Registration expiry: ' + reg_expiry
                if sn.state('last_reward_block_height') > sn.state('registration_height'):
                    reply_text += 'Last reward at height {} (approx. {})\n'.format(self.b(sn.state('last_reward_block_height')), ago(
                        AVERAGE_BLOCK_SECONDS * (height - sn.state('last_reward_block_height'))))
                else:
                    reply_text += 'Last reward: '+self.b('never')+'.\n'

                lrbh = sn.state('last_reward_block_height')
                # FIXME: this is slightly wrong because it doesn't account for other SNs that may expire
                # before they earn a reward:
                blocks_to_go = 1 + sum(1 for sni in sns.values() if
                        ('active' not in sni or sni['active']) and sni['total_contributed'] >= sni['staking_requirement'] and sni['last_reward_block_height'] < lrbh)

                if sn.decommissioned():
                    reply_text += 'Next reward: *never* (currently decommissioned)\n'
                else:
                    reply_text += 'Next reward in {} blocks (approx. {})\n'.format(self.b(blocks_to_go), friendly_time(blocks_to_go * AVERAGE_BLOCK_SECONDS))
            else:
                reply_text += 'Status: ' + sn.status_icon() + ' ' + self.b('awaiting contributions')+'\n'
                contr, req = sn.state('total_contributed'), sn.state('staking_requirement')
                reply_text += ('Stake: '+self.i('{:.9f}')+' ('+self.i('{:.1f}%')+' of required '+self.i('{:.9f}')+'; additional contribution required: {:.9f})\n').format(
                        contr/COIN, contr/req * 100, req/COIN, (req - contr)/COIN)
                reply_text += stakes
                reply_text += 'Operator fee: '+self.b('{:.1f}%'.format(sn.operator_fee() * 100))+'\n'
                reply_text += 'Registration expiry: ' + reg_expiry

            if 'id' in sn:
                reply_text += 'Reward notifications: ' + self.b(('en' if sn['rewards'] else 'dis') + 'abled') + '\n'
                reply_text += 'Close-to-expiry notifications: ' + self.b(('en' if sn['expires_soon'] else 'dis') + 'abled') + '\n'

        else:  # not active:
            if 'alias' in sn and sn['alias']:
                reply_text += 'Service node {} is not registered\n'.format(self.i(pubkey))
            else:
                reply_text += 'Not registered\n'

        if send:
            self.send_reply(reply_text)
        else:
            return (reply_text, sn)


    @abstractmethod
    def wallets_menu(self, reply_text=''):
        """Lists the known wallets"""
        pass


    def find_unmonitored(self):
        """Finds any unmonitored service nodes and starts monitoring them.  Returns the list of
        ServiceNodes that were added."""
        uid = self.get_uid()
        have = set()
        for sn in ServiceNode.all(uid):
            have.add(sn['pubkey'])

        cur = pgsql.cursor()
        cur.execute("SELECT wallet from wallet_prefixes WHERE uid = %s", (uid,))
        wallets = []
        for row in cur:
            wallets.append(row[0])
        wallets = tuple(wallets)

        added = []

        sns = lokisnbot.sn_states
        for pubkey, sn in sns.items():
            if pubkey in have:
                continue
            if any(c['address'].startswith(wallets) for c in sn['contributors']):
                have.add(pubkey)
                sn = ServiceNode({
                    'pubkey': pubkey,
                    'uid': uid,
                    'active': True,
                    'complete': sn['total_contributed'] >= sn['staking_requirement'],
                    'last_reward_block_height': sn['last_reward_block_height']
                })
                sn.insert()
                added.append(sn)

        return added


    def plain_input(self, text, add_sn=False):
        """Called when the bot is given plain input, which it expects to be SN ids to
        display/summarize and, possibly, to add (if `add_sn`).  Returns True if the message
        was good and responses were sent, None if the input couldn't be parsed."""

        uid = self.get_uid()

        if re.match('^\s*[0-9a-f]{64}(?:\s+[0-9a-f]{64})*\s*$', text):
            pubkeys = text.split()
        else:
            return None

        just_looking = True
        if add_sn:
            just_looking = False

        many = len(pubkeys) > 5

        summary = []
        for pubkey in pubkeys:
            sn = None
            try:
                sn = ServiceNode(uid=uid, pubkey=pubkey)
            except ValueError:
                pass

            append_status = True
            if not just_looking:
                if sn:
                    summary.append('I am '+self.i('already')+' monitoring service node '+self.i(sn.shortpub())+' for you.  Current status:')

                else:
                    sn_data = { 'pubkey': pubkey, 'uid': uid }
                    sns, tsns = lokisnbot.sn_states, lokisnbot.testnet_sn_states
                    if pubkey in sns:
                        sn_data['active'] = True
                        sn_data['complete'] = sns[pubkey]['total_contributed'] >= sns[pubkey]['staking_requirement']
                        sn_data['last_reward_block_height'] = sns[pubkey]['last_reward_block_height']
                        summary.append("Okay, I'm now monitoring service node "+self.i('{}')+" for you.  Current status:")
                    elif pubkey in tsns:
                        sn_data['active'] = True
                        sn_data['testnet'] = True
                        sn_data['complete'] = tsns[pubkey]['total_contributed'] >= tsns[pubkey]['staking_requirement']
                        sn_data['last_reward_block_height'] = tsns[pubkey]['last_reward_block_height']
                        summary.append("Okay, I'm now monitoring "+self.b('testnet')+" service node "+self.i('{}')+" for you.  Current status:")
                    else:
                        summary.append("Service node "+self.i('{}')+" isn't currently registered on the network, but I'll start monitoring it for you once it appears.")
                        append_status = False

                    sn = ServiceNode(sn_data)
                    sn.insert()
                    summary[-1] = summary[-1].format(sn.shortpub())
            elif sn:
                summary.append('Service node '+self.i(sn.shortpub())+' status:' if many else '')
            else:
                sn = ServiceNode({ 'pubkey': pubkey })
                if not many:
                    summary.append('')
                else:
                    summary.append('Service node '+self.i(sn.shortpub())+" current status:")

            if not many:
                if append_status:
                    self.service_node(sn=sn, reply_text=summary[-1])
                else:
                    self.send_reply(summary[-1])
                summary.clear()
            else:
                if append_status:
                    summary[-1] += ' ' + sn.status_icon() + ' '
                    if not sn.active():
                        summary[-1] += 'Expired/deregistered'
                    else:
                        summary[-1] += 'Active' if sn.active_on_network() else '☣ *DECOMMISSIONED*' if sn.decommissioned() else 'Awaiting registration'
                        if sn.proof_age() is None or sn.proof_age() > PROOF_AGE_WARNING:
                            summary[-1] += '; ' + self.b('last uptime proof: ' + sn.format_proof_age())
                        else:
                            summary[-1] += '; last uptime proof: ' + sn.format_proof_age()

                        if sn.infinite_stake() and sn.expiry_block() is None:
                            summary[-1] += '; ' + self.i('infinitely staked')
                        else:
                            summary[-1] += '; expires at block {} ({})'.format(self.b(sn.expiry_block()), self.i(friendly_time(sn.expires_in())))
                    summary[-1] += '.'

                if len(summary) >= 10:
                    self.send_reply(message='\n\n'.join(summary))
                    summary.clear()

        if many and summary:
            self.send_reply(message='\n\n'.join(summary))
            summary.clear()
        return True
