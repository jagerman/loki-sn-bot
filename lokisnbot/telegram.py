# Telegram-specific bits for loki-sn-bot

import re
import math

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ForceReply
from telegram.constants import ParseMode, ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, CallbackContext
import telegram.ext.filters
from telegram.error import TelegramError, BadRequest

import lokisnbot
from . import pgsql
from .constants import *
from .util import friendly_time, ago, explorer, escape_markdown
from .servicenode import ServiceNode
from .network import Network, NetworkContext



def common_symbol(all_sns):
    more_sym = ''
    for sn in all_sns:
        for sym in sn.status_icon():
            if sym not in more_sym:
                more_sym += sym
    return more_sym


class TelegramContext(NetworkContext):
    def __init__(self, update: Update, context: CallbackContext):
        self.update = update
        self.context = context


    @staticmethod
    def b(text):
        return '*{}*'.format(text)


    @staticmethod
    def i(text):
        return '_{}_'.format(text)


    @staticmethod
    def escape_msg(txt):
        return escape_markdown(txt)


    async def send_reply(self, message, reply_markup=None, dead_end=False, expect_reply=False):
        """Sends a reply.  reply_markup can be used to append buttons; dead_end can be used instead of
        reply_markup to add just a '<< Main menu' button; expect_reply puts the user into reply mode (only
        has effect if reply_markup and dead_end are omitted)."""
        if not reply_markup:
            if dead_end:
                reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton('<< Main menu', callback_data='main')]])
            elif expect_reply:
                reply_markup = ForceReply()

        if self.update.message:
            send = lambda msg, rmarkup: self.update.message.reply_markdown(msg,
                    reply_markup=rmarkup,
                    disable_web_page_preview=True)
        else:
            chat_id = self.update.callback_query.message.chat_id
            send = lambda msg, rmarkup: self.context.bot.send_message(
                chat_id=chat_id,
                text=msg, parse_mode=ParseMode.MARKDOWN,
                reply_markup=rmarkup,
                disable_web_page_preview=True)

        msgs = self.breakup_long_message(message, 4096)
        for msg in msgs[:-1]:
            await send(msg, None)
        await send(msgs[-1], reply_markup)


    def get_uid(self):
        """Returns the user id in the pg database; creates one if not already found"""
        if 'uid' not in self.context.user_data:
            cur = pgsql.cursor()
            cur.execute("SELECT id FROM users WHERE telegram_id = %s", (self.update.effective_user.id,))
            row = cur.fetchone()
            if row is None:
                cur.execute("INSERT INTO users (telegram_id) VALUES (%s) RETURNING id", (self.update.effective_user.id,))
                row = cur.fetchone()
            if row is None:
                return None
            self.context.user_data['uid'] = row[0]
        return self.context.user_data['uid']


    def want(self):
        if 'want' in self.context.user_data:
            return (self.context.user_data['want'], self.context.user_data['want_data'])
        else:
            return (None, None)


    def expect(self, what, data=None):
        self.context.user_data['want'] = what
        self.context.user_data['want_data'] = data


    def is_dm(self):
        return (
            self.update.message.chat.type == 'private' if self.update.message else
            self.update.callback_query.message.chat.type == 'private' if self.update.callback_query else
            False  # don't know!
            )


    async def main_menu(self, reply='', last_button=None, testnet_buttons=False):
        self.expect(None)

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
        if last_button:
            choices.append([last_button])
        else:
            choices.append([InlineKeyboardButton('Status', callback_data='status')])
            if lokisnbot.config.DONATION_ADDR:
                choices[-1].append(InlineKeyboardButton('Donate', callback_data='donate'))

        await super().main_menu(reply, reply_markup=InlineKeyboardMarkup(choices))


    async def start(self):
        return await self.main_menu(lokisnbot.config.WELCOME.format(owner=lokisnbot.config.TELEGRAM_OWNER), testnet_buttons=True)


    async def status(self, testnet=False):
        await super().status(testnet=testnet, last_button=InlineKeyboardButton('<< Main menu', callback_data='main'))


    async def testnet_status(self):
        return await self.status(testnet=True)


    async def testnet_faucet(self):
        """Asks the user for a testnet address to send faucet testnet loki."""
        if await self.faucet_was_recently_used():
            return
        await self.send_reply("So you want some "+self.b('testnet SESH')+"!  You've come to the right place: just send me your testnet address and I'll send some your way (use /start to cancel):",
                expect_reply=True)
        self.expect('faucet')
        return True


    async def turn_faucet(self):
        """Sends some testnet SESH.  Returns True if successful, False if failed, and None if it prompted the user to send the address again"""
        uid = self.get_uid()
        self.expect(None)
        if await self.faucet_was_recently_used():
            return

        wallet = self.update.message.text
        if self.is_wallet(wallet):
            await self.context.bot.send_chat_action(chat_id=self.update.message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)

            tx = self.send_faucet_tx(wallet)
            if tx:
                tx_hash = tx['tx_hash']
                await self.send_reply(dead_end=True, message='💸 Sent you {:.9f} testnet SESH in {}'.format(
                    lokisnbot.config.TESTNET_FAUCET_AMOUNT/COIN, '['+tx_hash[0:8]+'...](https://'+lokisnbot.config.TESTNET_EXPLORER+'/tx/'+tx_hash+')'))

        else:
            await self.send_reply(
                    '{} does not look like a valid ETH wallet address!  Please check the address and send it again (use /start to cancel):'.format(wallet),
                    expect_reply=True)
            self.expect('faucet')


    async def service_nodes_menu(self, reply_text='', page=0):
        buttons = []
        uid = self.get_uid()
        all_sns = ServiceNode.all(uid)
        any_rewards_enabled = False

        ncols = 3 if len(all_sns) > 16 else 2 if len(all_sns) >= 6 else 1

        # If we have over 60 then starting paging the results.  (We could technically show up to 95
        # because Telegram allows 100 buttons, but that is really hard to read with tiny buttons or
        # a huge long list).
        #
        # If we are going to page, then we change sort order to put everything that *isn't* in good
        # status first, then we put "<< + xx more" and/or "+ xx more >>" buttons.
        if len(all_sns) > 60:
            max_page = (len(all_sns) + 59) // 60 - 1
            all_sns.sort(key=lambda sn: 0 if sn.status_icon()[-1] != '💚' else 1)
        else:
            max_page = 0

        if page > max_page:
            page = 0

        prev_button = None
        if page > 0:
            more_sym = common_symbol(all_sns[0:60*page])
            prev_button = InlineKeyboardButton("<< {} + {} more".format(more_sym, 60*page), callback_data='sns_page{}'.format(page-1))
            all_sns = all_sns[60*page:]

        for sn in all_sns[0:60]:
            snbutton = InlineKeyboardButton(
                    sn.status_icon() + ' ' + ('{} ({})'.format(sn.alias(), sn.shortpub()) if sn['alias'] else sn.shortpub()),
                    callback_data='sn:{}'.format(sn['id']))
            if buttons and len(buttons[-1]) < ncols:
                buttons[-1].append(snbutton)
            else:
                buttons.append([snbutton])
            if sn['rewards']:
                any_rewards_enabled = True

        if prev_button:
            buttons.append([prev_button])
        if max_page > page:
            more_sym = common_symbol(all_sns[60:])
            next_button = InlineKeyboardButton("{} + {} more >>".format(more_sym, len(all_sns)-60), callback_data='sns_page{}'.format(page+1))
            if not prev_button:
                buttons.append([])
            buttons[-1].append(next_button)

        buttons.append([InlineKeyboardButton('Add a service node', callback_data='add_sn'),
            InlineKeyboardButton('Show versions/expiries/proofs', callback_data='sns_expiries')]);
        buttons.append([
            InlineKeyboardButton('Find unmonitored SNs', callback_data='find_unmonitored_sn'),
            InlineKeyboardButton('Disable reward notifications', callback_data='disable_rewards_all')
                if any_rewards_enabled else
                InlineKeyboardButton('Enable reward notifications', callback_data='enable_rewards_all')
        ])
        buttons.append([InlineKeyboardButton('<< Main menu', callback_data='main')])

        sn_menu = InlineKeyboardMarkup(buttons)
        if reply_text:
            reply_text += '\n\n'
        reply_text += 'View an existing service node, or add a new one?'

        await self.send_reply(reply_text, reply_markup=sn_menu)


    async def service_nodes_expiries(self):
        uid = self.get_uid()
        sns = ServiceNode.all(uid, sortkey=lambda sn: (sn['testnet'], sn.expiry_block() or float("inf"), sn['alias'] or sn['pubkey']))

        height = lokisnbot.network_info['height']
        msg = self.b('Service node versions, expirations & proofs:')+'\n'
        testnet = False
        for sn in sns:
            if not testnet and sn['testnet']:
                msg += '\n'+self.b('Testnet service node versions, expirations & proofs:')+'\n'
                height = lokisnbot.testnet_network_info['height']
                testnet = True

            msg += '{} {}: '.format(sn.status_icon(), sn.alias())
            if not sn.active():
                msg += 'Expired/deregistered\n'
            else:
                msg += '*v{}*'.format(sn.version_str() or 'Unknown')
                if sn.infinite_stake() and sn.expiry_block() is None:
                    msg += '; ∞ stake'
                else:
                    msg += '; block _{}_ (_{}_)'.format(sn.expiry_block(), friendly_time(sn.expires_in()))
                if not sn.solo():
                    msg += '; _{:.3g}%_ fee'.format(sn.operator_fee() * 100)

                msg += '; ' + sn.format_proof_age(extra_short=True)
                msg += '\n'

        await self.service_nodes_menu(reply_text=msg)


    async def service_node_add(self):
        await self.send_reply('Okay, send me the public key(s) of the service node(s) to add (use /start to cancel):', expect_reply=True)
        self.expect('add_sn')


    async def service_node_menu(self):
        return await self.service_node(snid=int(self.update.callback_query.data.split(':', 1)[1]))


    async def service_node_menu_inplace(self):
        snid = self.update.callback_query.data.split(':', 1)[1]
        sn = None
        if snid == 'last':
            snid = None
            sn = ServiceNode({ 'pubkey': self.context.user_data['sn_last_viewed'] })
            del self.context.user_data['sn_last_viewed']
        msg, sn = await self.service_node(snid=snid, sn=sn, send=False)
        try:
            await self.context.bot.edit_message_text(
                    text=msg, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=self.sn_markup_menu(sn),
                    chat_id=self.update.callback_query.message.chat_id,
                    message_id=self.update.callback_query.message.message_id)
        except BadRequest as e:
            if 'Message is not modified' in e.message:
                pass
            else:
                raise


    async def plain_input(self, text=None):
        if text is None:
            text = self.update.message.text

        uid = self.get_uid()
        want, want_data = self.want()
        if want == 'note':
            self.expect(None)
            snid = want_data
            pgsql.cursor().execute("UPDATE service_nodes SET note = %s WHERE id = %s AND uid = %s", (text, snid, uid))
            return await self.service_node(snid=snid, reply_text='Updated note for '+self.i('{alias}')+'.  Current status:')

        elif want == 'alias':
            self.expect(None)
            snid = want_data
            alias = text.replace("*", "").replace("_", "").replace("[", "").replace("`", "")
            pgsql.cursor().execute("UPDATE service_nodes SET alias = %s WHERE id = %s AND uid = %s", (alias, snid, uid))
            return await self.service_node(snid=snid, reply_text="Okay, I'll now refer to service node "+self.i('{sn[pubkey]}')+' as '+self.i('{sn[alias]}')+'.  Current status:')

        elif want == 'wallet':
            wallet = self.update.message.text
            if not self.is_wallet(wallet, partial=True):
                await self.send_reply('That doesn\'t look like a valid wallet address.  Send me at least the first {} characters of your wallet address (use /start to cancel):'.format(
                    lokisnbot.config.PARTIAL_WALLET_MIN_LENGTH), expect_reply=True)
                return
            self.expect(None)
            pgsql.cursor().execute("INSERT INTO wallet_prefixes (uid, wallet) VALUES (%s, %s) ON CONFLICT DO NOTHING", (uid, wallet))
            return await self.wallets_menu(('Added {}wallet '+self.i('{}')+'.  I\'ll now calculate your share of shared contribution service node rewards.').format(
                self.b('testnet')+' ' if wallet[0] == 'T' else '', wallet))

        elif want == 'faucet':
            return self.turn_faucet()

        # Otherwise we're expecting pubkeys, either for query or for adding
        add_sn = want == 'add_sn'

        if await super().plain_input(text, add_sn=add_sn):
            self.expect(None)
        elif add_sn:
            await self.send_reply(message="That doesn't look like a valid service node public key; please check the key(s) and try again: (use /start to cancel)", expect_reply=True)
        elif text == 'myid':
            await self.send_reply(message='Your telegram id is: {}; your internal LokiSNBot id is {}'.format(self.update.effective_user.id, uid))
        else:
            await self.send_reply(dead_end=True, message='Sorry, I didn\'t understand your message.')


    def sn_markup_menu(self, sn):
        expurl = explorer(sn.testnet)
        if 'id' not in sn:  # If it has no row id then it isn't something this user is watching yet
            self.context.user_data['sn_last_viewed'] = sn['pubkey']
            return InlineKeyboardMarkup([
                [InlineKeyboardButton('Refresh', callback_data='refresh:last'),
                 InlineKeyboardButton('View on '+expurl, url='https://{}/sn/{}'.format(expurl, sn['pubkey']))],
                [InlineKeyboardButton('Start monitoring {}'.format(sn.alias()), callback_data='start:last')],
                [InlineKeyboardButton('< Service nodes', callback_data='sns'), InlineKeyboardButton('<< Main menu', callback_data='main')]
                ])
        else:
            snid = sn['id']
            return InlineKeyboardMarkup([
                [InlineKeyboardButton('Refresh', callback_data='refresh:{}'.format(snid)),
                 InlineKeyboardButton('View on '+expurl, url='https://{}/sn/{}'.format(expurl, sn['pubkey']))],
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


    async def service_node(self, *, send=True, **kwargs):
        reply_text, sn = await super().service_node(**kwargs, send=False)

        if send:
            await self.send_reply(reply_text, reply_markup=self.sn_markup_menu(sn))
        else:
            return (reply_text, sn)


    async def start_monitoring(self):
        self.expect('add_sn')
        pubkey = self.context.user_data['sn_last_viewed']
        del self.context.user_data['sn_last_viewed']
        await self.plain_input(text=pubkey)


    async def stop_monitoring(self):
        uid = self.get_uid()
        snid = int(self.update.callback_query.data.split(':', 1)[1])
        try:
            sn = ServiceNode(snid=snid, uid=uid)
        except ValueError:
            return await self.service_nodes_menu("I couldn't find that service node; please try again")
        sn.delete()
        msg = "Okay, I'm no longer monitoring service node " + (
                "_{}_ (_{}_)".format(sn['alias'], sn['pubkey']) if sn['alias'] else "_{}_".format(sn['pubkey'])) + " for you."
        return await self.service_nodes_menu(msg)


    async def request_sn_field(self, field, send_fmt, current_fmt):
        uid = self.get_uid()
        snid = int(self.update.callback_query.data.split(':', 1)[1])
        try:
            sn = ServiceNode(snid=snid, uid=uid)
        except ValueError:
            return await self.send_reply(dead_end=True, message="I couldn't find that service node!")

        self.expect(field, snid)
        msg = send_fmt.format(sn=sn, alias=sn.alias())
        if sn[field]:
            msg += '\n\n' + current_fmt.format(sn[field], escaped=self.escape_msg(sn[field]))
        await self.send_reply(msg, expect_reply=True)


    async def set_sn_field(self, field, value, success):
        uid = self.get_uid()
        snid = int(self.update.callback_query.data.split(':', 1)[1])
        try:
            sn = ServiceNode(snid=snid, uid=uid)
        except ValueError:
            return await self.service_node(snid=snid, reply_text="I couldn't find that service node!")

        sn.update(**{field: value})

        await self.service_node(sn=sn, reply_text=success.format(sn.alias()))


    async def ask_note(self):
        await self.request_sn_field('note',
                "Send me a custom note to set for service node _{alias}_ (use /start to cancel).",
                "The current note is: {escaped}")


    async def del_note(self):
        await self.set_sn_field('note', None, 'Removed note for service node _{}_.')


    async def ask_alias(self):
        await self.request_sn_field('alias',
                "Send me an alias to use for this service node instead of the public key (_{sn[pubkey]}_).  Use /start to cancel.",
                "The current alias is: {}")


    async def del_alias(self):
        await self.set_sn_field('alias', None, 'Removed alias for service node _{}_.')


    async def enable_reward_notify(self):
        await self.set_sn_field('rewards', True,
                "Okay, I'll start sending you block reward notifications for _{}_.")


    async def disable_reward_notify(self):
        await self.set_sn_field('rewards', False,
                "Okay, I'll no longer send you block reward notifications for _{}_.")


    async def enable_reward_notify_all(self):
        uid = self.get_uid()
        all_sns = ServiceNode.all(uid)
        enabled_for = []
        for sn in all_sns:
            if not sn['rewards']:
                sn.update(rewards=True)
                enabled_for.append("_{}_".format(sn.alias()))

        await self.service_nodes_menu('Reward notification *enabled* for service nodes {}.'.format(", ".join(enabled_for)))


    async def disable_reward_notify_all(self):
        uid = self.get_uid()
        all_sns = ServiceNode.all(uid)
        disabled_for = []
        for sn in all_sns:
            if sn['rewards']:
                sn.update(rewards=False)
                disabled_for.append("_{}_".format(sn.alias()))

        await self.service_nodes_menu('Reward notification *disabled* for service nodes {}.'.format(", ".join(disabled_for)))


    async def enable_expires_soon(self):
        await self.set_sn_field('expires_soon', True,
                "Okay, I'll send you expiry notifications when _{}_ is close to expiry (48h, 24h, and 6h).")


    async def disable_expires_soon(self):
        await self.set_sn_field('expires_soon', False,
                "Okay, I'll stop sending you notifications when _{}_ is close to expiry.")


    async def wallets_menu(self, reply_text=''):
        uid = self.get_uid()

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

        if wallets:
            wallets.append([
                InlineKeyboardButton('Find unmonitored SNs', callback_data='find_unmonitored'),
                InlineKeyboardButton('Disable auto-monitoring', callback_data='disable_automon')
                if self.get_user_field('auto_monitor') else
                InlineKeyboardButton('Enable auto-monitoring', callback_data='enable_automon'),
                ])

        wallets.append([InlineKeyboardButton('Add a wallet', callback_data='ask_wallet'),
            InlineKeyboardButton('<< Main menu', callback_data='main')])

        w_menu = InlineKeyboardMarkup(wallets)
        if reply_text:
            reply_text += '\n\n'
        reply_text += ('If you let me know your wallet address(es) I can calculate your specific reward and your portion '
                'of the stake (for shared contribution service nodes).  I can also use it to automatically monitor new SNs '
                'that you register or contribute to.')

        await self.send_reply(reply_text, reply_markup=w_menu)


    async def forget_wallet(self):
        w = self.update.callback_query.data.split(':', 1)[1]

        uid = self.get_uid()
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

        return await self.wallets_menu('\n'.join(msgs))


    async def ask_wallet(self):
        self.expect('wallet')
        msg = 'You can either send the whole wallet address or, if you prefer, just the first 7 (or more) characters of the wallet address.  To register a wallet, send it to me now.  Use /start to cancel.'
        await self.send_reply(msg, expect_reply=True)


    async def find_unmonitored(self, on_none=None):
        if on_none is None:
            on_none = self.wallets_menu
        added = super().find_unmonitored()
        if added:
            return await self.service_nodes_menu(
                '\n'.join('Found and added {} {}.'.format(sn.status_icon(), sn.alias()) for sn in added))
        else:
            return await on_none("Didn't find any unmonitored service nodes matching your wallet(s).")


    async def set_automon(self, enabled : bool):
        enabled = self.set_user_field('auto_monitor', enabled)
        await self.wallets_menu(
                'Automatic monitoring of for service nodes you have contributed to is now ' + self.b(
                    "enabled" if enabled else "disabled"))


    async def donate(self):
        chat_id = self.update.callback_query.message.chat_id
        msg = 'Find this bot useful?  Donations appreciated: ' + lokisnbot.config.DONATION_ADDR
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton('<< Main menu', callback_data='main')]])
        if lokisnbot.config.DONATION_IMAGE:
            await self.context.bot.send_photo(
                chat_id=chat_id,
                photo=open(lokisnbot.config.DONATION_IMAGE, 'rb'),
                caption=msg,
                reply_markup=reply_markup)
        else:
            await self.context.bot.send_message(
                chat_id=chat_id,
                text=message,
                reply_markup=reply_markup)


    def error(self):
        """Log Errors caused by Updates."""
        lokisnbot.logger.warning('Update "%s" caused error "%s"', self.update, self.context.error)


    async def dispatch_query(self):
        q = self.update.callback_query.data
        edit = True
        call = None
        if q == 'main':
            call = self.start
        elif q == 'sns':
            call = self.service_nodes_menu
        elif (match := re.match(r'sns_page(\d)', q)):
            call = lambda: self.service_nodes_menu(page=int(match[1]))
        elif q == 'sns_expiries':
            call = self.service_nodes_expiries
        elif q == 'status':
            call = self.status
        elif q == 'testnet_status':
            call = self.testnet_status
        elif q == 'testnet_faucet':
            call = self.testnet_faucet
        elif q == 'add_sn':
            call = self.service_node_add
        elif re.match(r'sn:\d+', q):
            call = self.service_node_menu
        elif re.match(r'refresh:(?:\d+|last)', q):
            edit = False
            call = self.service_node_menu_inplace
        elif q == 'start:last':
            call = self.start_monitoring
        elif re.match(r'stop:\d+', q):
            call = self.stop_monitoring
        elif re.match(r'alias:\d+', q):
            call = self.ask_alias
        elif re.match(r'del_alias:\d+', q):
            call = self.del_alias
        elif re.match(r'note:\d+', q):
            call = self.ask_note
        elif re.match(r'del_note:\d+', q):
            call = self.del_note
        elif re.match(r'enable_reward:\d+', q):
            call = self.enable_reward_notify
        elif re.match(r'disable_reward:\d+', q):
            call = self.disable_reward_notify
        elif q == 'enable_rewards_all':
            call = self.enable_reward_notify_all
        elif q == 'disable_rewards_all':
            call = self.disable_reward_notify_all
        elif re.match(r'enable_expires_soon:\d+', q):
            call = self.enable_expires_soon
        elif re.match(r'disable_expires_soon:\d+', q):
            call = self.disable_expires_soon
        elif q == 'wallets':
            call = self.wallets_menu
        elif re.match(r'forget_wallet:\w+', q):
            call = self.forget_wallet
        elif q == 'ask_wallet':
            call = self.ask_wallet
        elif q == 'find_unmonitored':
            call = self.find_unmonitored
        elif q == 'find_unmonitored_sn':
            call = lambda: self.find_unmonitored(self.service_nodes_menu)
        elif q == 'enable_automon':
            call = lambda: self.set_automon(True)
        elif q == 'disable_automon':
            call = lambda: self.set_automon(False)
        elif q == 'donate':
            call = self.donate

        if edit:
            try:
                await self.context.bot.edit_message_reply_markup(reply_markup=None,
                        chat_id=self.update.callback_query.message.chat_id,
                        message_id=self.update.callback_query.message.message_id)
            except BadRequest as e:
                if 'Message is not modified' in e.message:
                    pass
                else:
                    raise
        if call:
            return await call()


def context_handler(ctx_method):
    def handler(update: Update, context: CallbackContext):
        return ctx_method(TelegramContext(update, context))
    return handler


class TelegramNetwork(Network):
    def __init__(self, **kwargs):
        self.app = ApplicationBuilder().token(lokisnbot.config.TELEGRAM_TOKEN).build()

        self.app.add_handler(CommandHandler('start', context_handler(TelegramContext.start)))
        self.app.add_handler(CallbackQueryHandler(context_handler(TelegramContext.dispatch_query)))
        self.app.add_handler(MessageHandler(telegram.ext.filters.TEXT, context_handler(TelegramContext.plain_input)))

        # log all errors
        self.app.add_error_handler(context_handler(TelegramContext.error))

    def start(self, loop):
        loop.run_until_complete(self.app.initialize())
        if self.app.post_init:
            loop.run_until_complete(self.app.post_init(self.app))

        if lokisnbot.config.TELEGRAM_WEBHOOK_URL and lokisnbot.config.TELEGRAM_WEBHOOK_PORT:
            loop.run_until_complete(self.app.updater.start_webhook(
                    listen='127.0.0.1', url_path='/', port=lokisnbot.config.TELEGRAM_WEBHOOK_PORT,
                    webhook_url=lokisnbot.config.TELEGRAM_WEBHOOK_URL))
        else:
            loop.run_until_complete(self.app.updater.start_polling())

        loop.run_until_complete(self.app.start())


    async def stop(self):
        if self.app.updater.running:
            await self.app.updater.stop()
        if self.app.running:
            await self.app.stop()
        if self.app.post_stop:
            await self.app.post_stop(self.app)
        await self.app.shutdown()
        if self.app.post_shutdown:
            await self.app.post_shutdown(self.app)

    async def try_message(self, chatid, message, reply_markup=None):
        """Send a message to the bot.  If the message gives a 'bot was blocked by the user' error then
        we delete the user's service_nodes (to stop generating more messages)."""
        try:
            await self.app.bot.send_message(chatid, message, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        except TelegramError as e:
            if any(x in e.message for x in ('bot was blocked by the user', 'user is deactivated')):
                print("Telegram user {} blocked me or is no longer active; removing them from SN monitoring ({})".format(chatid, e), flush=True)

                pgsql.cursor().execute("DELETE FROM service_nodes WHERE uid = (SELECT id FROM users WHERE telegram_id = %s)", (chatid,))
            else:
                print("Error sending Telegram message to {}: {}".format(chatid, e), flush=True)
            return False

        return True

    def sn_update_extra(self, sn):
        expurl = explorer(testnet=sn.testnet)
        return {
                'reply_markup': InlineKeyboardMarkup([[
                    InlineKeyboardButton('SN details', callback_data='sn:{}'.format(sn['id'])),
                    InlineKeyboardButton(expurl, url='https://{}/sn/{}'.format(expurl, sn['pubkey'])),
                    InlineKeyboardButton('<< Main menu', callback_data='main')
                    ]])
                }

    def ready(self):
        return self.app.running
