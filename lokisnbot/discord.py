# Discord-specific bits for loki-sn-bot

import re
import math
import threading
import asyncio

import discord
from discord.ext import commands

import lokisnbot
from . import pgsql
from .constants import *
from .util import friendly_time, ago, explorer, escape_markdown
from .servicenode import ServiceNode
from .network import Network, NetworkContext

uid_cache = {}
last_pubkeys = {}

message_futures = []

class DiscordContext(NetworkContext):
    def __init__(self, context: commands.Context):
        self.context = context


    @staticmethod
    def b(text):
        return '**{}**'.format(text)


    @staticmethod
    def i(text):
        return '*{}*'.format(text)


    @staticmethod
    def escape_msg(txt):
        return escape_markdown(txt)

    async def send_reply_async(self, message, **kwargs):
        await self.context.send(message, **kwargs)

    def send_reply(self, message, dead_end=False, expect_reply=False, **kwargs):
        asyncio.ensure_future(self.send_reply_async(message, **kwargs))


    def get_uid(self):
        """Returns the user id in the pg database; creates one if not already found"""
        global uid_cache
        uid = self.context.author.id
        if uid not in uid_cache:
            cur = pgsql.cursor()
            cur.execute("SELECT id FROM users WHERE discord_id = %s", (uid,))
            row = cur.fetchone()
            if row is None:
                cur.execute("INSERT INTO users (discord_id) VALUES (%s) RETURNING id", (uid,))
                row = cur.fetchone()
            if row is None:
                return None
            uid_cache[uid] = row[0]
        return uid_cache[uid]


    def is_dm(self):
        return isinstance(self.context.channel, discord.DMChannel)


    def start(self):
        return self.main_menu(lokisnbot.config.WELCOME.format(owner=lokisnbot.config.DISCORD_OWNER), testnet_buttons=True)


    def service_nodes(self, reply_text=''):
        global last_pubkeys
        uid = self.get_uid()
        all_sns = ServiceNode.all(uid)

        last_pubkeys[uid] = []
        if reply_text:
            reply_text += '\n\n'

        if all_sns:
            for i, sn in enumerate(all_sns):
                last_pubkeys[uid].append(sn['pubkey'])
                reply_text += '{} — {} {}\n'.format(self.b(i+1), sn.status_icon(), ('{} *({})*'.format(sn.alias(), sn.shortpub()) if sn['alias'] else sn.shortpub()))

            reply_text += '\n(Note: you can use the indices above for commands expecting a pubkey.  For example `$sn 1` to view SN 1 details or `$alias 2` changes the alias of SN 2)'
        else:
            reply_text += 'No service nodes are currently monitored.'

        self.send_reply(reply_text)


    def service_nodes_expiries(self):
        global last_pubkeys
        uid = self.get_uid()
        sns = ServiceNode.all(uid, sortkey=lambda sn: (sn['testnet'], sn.expiry_block() or float("inf"), sn['alias'] or sn['pubkey']))

        last_pubkeys[uid] = []
        height = lokisnbot.network_info['height']
        if all_sns:
            msg = self.b('Service node expirations:')+'\n'
            testnet = False

            for i, sn in enumerate(sns):
                last_pubkeys[uid].append(sn['pubkey'])
                if not testnet and sn['testnet']:
                    msg += '\n'+self.b('Testnet service node expirations:')+'\n'
                    height = lokisnbot.testnet_network_info['height']
                    testnet = True

                msg += '{} — {} {}: {}\n'.format(self.b(i+1), sn.status_icon(), sn.alias(),
                        'Expired/deregistered' if not sn.active() else
                        'Never (infinite stake)' if sn.infinite_stake() and sn.expiry_block() is None else
                        'Block *{}* (*{}*)'.format(sn.expiry_block(), friendly_time(sn.expires_in())))
        else:
            msg += 'No service nodes are current monitored'

        self.send_reply(msg)


    def pubkey_from_arg(self, arg, send_errmsg=False):
        global last_pubkeys
        if re.search('^[0-9a-f]{64}$', arg):
            return arg
        if self.is_dm():
            uid = self.get_uid()
            last_pks = last_pubkeys[uid] if uid in last_pubkeys else []
            if last_pks and re.search(r'^[1-9]\d*$', arg):
                arg = int(arg) - 1
                if arg < len(last_pks):
                    return last_pks[arg]
            if send_errmsg:
                self.send_reply("Error: `{}` is not a valid service node pubkey or list index".format(arg+1))
        elif send_errmsg:
            self.send_reply("Error: `{}` is not a valid service node pubkey".format(arg))
        return None


    def start_monitoring(self, *pubkeys : str):
        pubkeys = [self.pubkey_from_arg(x) for x in pubkeys]
        if None in pubkeys:
            return self.send_reply("Invalid usage: $start PUBKEY PUBKEY ... — starts monitoring one or more service nodes")
        self.plain_input(text=' '.join(pubkeys), add_sn=True)


    def stop_monitoring(self, *pubkeys : str):
        pubkeys = [self.pubkey_from_arg(x) for x in pubkeys]
        if None in pubkeys:
            return self.send_reply("Invalid usage: $stop PUBKEY PUBKEY ... — stop monitoring one or more service nodes")

        uid = self.get_uid()
        for pubkey in pubkeys:
            try:
                sn = ServiceNode(pubkey=pubkey, uid=uid)
            except ValueError:
                self.send_reply("I couldn't find service node {}, or I wasn't monitoring it.  Please check the public service node id and try again".format(pubkey))
            sn.delete()
            self.send_reply("Okay, I'm not longer monitoring service node " + (
                "{} ({})".format(self.i(sn['alias']), self.i(sn['pubkey'])) if sn['alias'] else self.i(sn['pubkey'])) + " for you.")


    async def get_response_from_user(self):
        global message_futures
        future = asyncio.get_event_loop().create_future()
        message_futures.append((future, lambda r: r.author == self.context.author and r.channel == self.context.channel))
        await future
        return future.result()


    async def request_sn_field(self, field, pubkey, send_fmt=None, current_fmt=None, success_fmt=None):
        pubkey = self.pubkey_from_arg(pubkey, send_errmsg=True)
        if pubkey is None:
            return

        try:
            sn = ServiceNode(pubkey=pubkey, uid=self.get_uid())
        except ValueError:
            return await self.send_reply_async(dead_end=True, message="I couldn't find that service node!")

        if send_fmt is None:
            send_fmt = "Send me a"+('n ' if any(field.startswith(v) for v in 'aeiou') else ' ') + field + " to set for service node _{alias}_."
        elif current_fmt is None:
            current_fmt = "The current "+field+" is: {escaped}"
        if success_fmt is None:
            success_fmt = 'Updated '+field+' for '+self.i('{}')+'.  Current status:'

        msg = send_fmt.format(sn=sn, alias=sn.alias())
        if sn[field]:
            msg += '\n\n' + current_fmt.format(sn[field], escaped=self.escape_msg(sn[field]))
        await self.send_reply_async(msg)
        response = await self.get_response_from_user()
        sn.update(**{field: response.content})
        self.service_node(sn=sn, reply_text=success_fmt.format(sn.alias()))


    def set_sn_field(self, field, pubkey, value, success):
        if pubkey == 'all':
            sns = ServiceNode.all(self.get_uid())
            if not sns:
                return self.send_reply("Unable to do that: you aren't currently monitoring any service nodes!")
        else:
            pubkey = self.pubkey_from_arg(pubkey, send_errmsg=True)
            if pubkey is None:
                return

            try:
                sn = ServiceNode(pubkey=pubkey, uid=self.get_uid())
            except ValueError:
                return self.service_node(snid=snid, reply_text="I couldn't find that service node!")

        success_msgs = []
        for sn in sns:
            sn.update(**{field: value})

            success_msgs.append(success.format(sn.alias()))

        if len(sns) == 1:
            self.service_node(sn=sns[0], reply_text=success.format(sn.alias()))
        else:
            self.service_nodes('\n'.join(success_msgs))


    def wallets_menu(self, reply_text=''):
        uid = self.get_uid()

        wallets = []

        cur = pgsql.cursor()
        cur.execute("SELECT wallet from wallet_prefixes WHERE uid = %s ORDER BY wallet", (uid,))
        for row in cur:
            w = row[0]
            prefix = '🚧' if w.startswith('T') else ''
            wallets.append(prefix + w)

        if reply_text:
            reply_text += '\n\n'
        reply_text += 'Known wallets:' + ('\n' + '\n'.join(wallets) if wallets else ' ' + self.i('none'))
        self.send_reply(reply_text)


    def forget_wallet(self, wallet_prefix : str):
        uid = self.get_uid()
        msgs = []
        remove = []
        cur = pgsql.cursor()
        cur.execute("SELECT wallet from wallet_prefixes WHERE uid = %s ORDER BY wallet", (uid,))
        for row in cur:
            if row[0].startswith(wallet_prefix):
                remove.append(row[0])
        cur.execute("DELETE FROM wallet_prefixes WHERE uid = %s AND wallet IN %s RETURNING wallet",
                (uid, tuple(remove)))
        for x in cur:
            msgs.append('Okay, I forgot about wallet _{}_.'.format(x[0]))

        if not msgs:
            msgs.append('I didn\'t know about that wallet in the first place!')

        self.wallets_menu('\n'.join(msgs))


    async def ask_wallet(self, wallet : str=None):
        if not wallet:
            msg = 'You can either send the whole wallet address or, if you prefer, just the first {} (or more) characters of the wallet address.  To register a wallet, send it to me now.'.format(
                    lokisnbot.config.PARTIAL_WALLET_MIN_LENGTH)
            await self.send_reply_async(msg)
            response = await self.get_response_from_user()
            wallet = response.content

        if not self.is_wallet(wallet, mainnet=True, testnet=True, primary=True, partial=True):
            await self.send_reply_async('That doesn\'t look like a valid primary wallet address!')
            return

        pgsql.cursor().execute("INSERT INTO wallet_prefixes (uid, wallet) VALUES (%s, %s) ON CONFLICT DO NOTHING", (self.get_uid(), wallet))
        return self.wallets_menu(('Added {}wallet '+self.i('{}')+'.  I\'ll now calculate your share of shared contribution service node rewards.').format(
            self.b('testnet')+' ' if wallet[0] == 'T' else '', wallet))


    def find_unmonitored(self):
        added = super().find_unmonitored()
        if added:
            return self.service_nodes(
                '\n'.join('Found and added {} {}.'.format(sn.status_icon(), sn.alias()) for sn in added))
        else:
            return self.wallets_menu("Didn't find any unmonitored service nodes matching your wallet(s).")


    async def turn_faucet(self, wallet : str=None):
        """Sends some testnet LOKI, or replies with an error message"""
        uid = self.get_uid()
        if self.faucet_was_recently_used():
            return

        if not wallet:
            msg = "So you want some "+self.b('testnet LOKI')+"!  You've come to the right place: just send me your testnet address and I'll send some your way:"
            await self.send_reply_async(msg)
            response = await self.get_response_from_user()
            wallet = response.content

        if self.is_wallet(wallet, mainnet=True, testnet=False):
            self.send_reply("🤣 Nice try, but I don't have any mainnet LOKI.  Try again with a "+self.i('testnet')+" wallet address instead")

        elif self.is_wallet(wallet, mainnet=False, testnet=True):
            tx = self.send_faucet_tx(wallet)
            if tx:
                self.send_reply(dead_end=True, message='💸 Sent you {:.9f} testnet LOKI: {}'.format(
                    lokisnbot.config.TESTNET_FAUCET_AMOUNT/COIN, 'https://'+lokisnbot.config.TESTNET_EXPLORER+'/tx/'+tx['tx_hash']))

        else:
            self.send_reply(
                    '{} does not look like a valid LOKI testnet wallet address!  Please check the address and try again'.format(wallet))


    def donate(self):
        msg = 'Find this bot useful?  Donations appreciated: ' + lokisnbot.config.DONATION_ADDR
        self.send_reply(msg, file=discord.File(open(lokisnbot.config.DONATION_IMAGE, 'rb')) if lokisnbot.config.DONATION_IMAGE else None)


class DiscordNetwork(Network):
    def __init__(self, **kwargs):
        helpcmd = commands.DefaultHelpCommand(dm_help=True, verify_checks=False)
        self.bot = commands.Bot(
                command_prefix='$',
                description='Loki Service Node status and monitoring bot',
                help_command=helpcmd
        )
        self.loop = asyncio.get_event_loop()

        dm_only = commands.check(lambda ctx: isinstance(ctx.channel, discord.DMChannel))

        class General(commands.Cog, name='General commands'):
            @commands.command()
            @dm_only
            async def about(self, ctx):
                """Shows welcome message and general bot info"""
                DiscordContext(ctx).start()

            @commands.command()
            async def status(self, ctx):
                """Shows current Loki network status"""
                DiscordContext(ctx).status()

            if lokisnbot.config.TESTNET_NODE_URL:
                @commands.command()
                async def testnet(self, ctx):
                    """Shows current testnet status"""
                    DiscordContext(ctx).status(testnet=True)

            if lokisnbot.config.TESTNET_WALLET_URL and lokisnbot.config.TESTNET_FAUCET_AMOUNT:
                @commands.command()
                @dm_only
                async def faucet(self, ctx, wallet : str=''):
                    """Request some testnet LOKI from the bot"""
                    await DiscordContext(ctx).turn_faucet(wallet)

            @commands.command()
            async def donate(self, ctx):
                """Like this bot?  Find out how to donate here"""
                DiscordContext(ctx).donate()

        class SNCommands(commands.Cog, name='Service node commands'):
            @commands.command()
            @dm_only
            async def expires(self, ctx):
                """Show service nodes sorted by expiry"""
                DiscordContext(ctx).service_nodes_expiries()

            @commands.command()
            @dm_only
            async def start(self, ctx, *pubkeys : str):
                """Adds a service node to the monitored service nodes"""
                DiscordContext(ctx).start_monitoring(*pubkeys)

            @commands.command()
            @dm_only
            async def stop(self, ctx, *pubkeys : str):
                """Stops monitoring service nodes"""
                DiscordContext(ctx).stop_monitoring(*pubkeys)

            @commands.command()
            @dm_only
            async def note(self, ctx, pubkey : str):
                """Adds a custom note for the service node"""
                c = DiscordContext(ctx)
                await c.request_sn_field('note', pubkey)

            @commands.command()
            @dm_only
            async def nonote(self, ctx, pubkey : str):
                """Deletes the custom note associated with a service node"""
                c = DiscordContext(ctx)
                c.set_sn_field('note', pubkey, None, 'Removed note for service node _{}_.')

            @commands.command()
            @dm_only
            async def alias(self, ctx, pubkey : str):
                """Assigns an alias to the service node"""
                c = DiscordContext(ctx)
                await c.request_sn_field('alias', pubkey,
                        "Send me an alias to use for this service node instead of the public key (_{sn[pubkey]}_).")

            @commands.command()
            @dm_only
            async def noalias(self, ctx, pubkey : str):
                """Removes the alias from a service node"""
                c = DiscordContext(ctx)
                c.set_sn_field('alias', pubkey, None, 'Removed alias for service node _{}_.')


            @commands.command()
            @dm_only
            async def rewards(self, ctx, pubkey : str):
                """Enables reward notification for a service node"""
                c = DiscordContext(ctx)
                c.set_sn_field('rewards', pubkey, True,
                        "Okay, I'll start sending you block reward notifications for _{}_.")

            @commands.command()
            @dm_only
            async def norewards(self, ctx, pubkey : str):
                """Disables reward notifications for a service node"""
                c = DiscordContext(ctx)
                c.set_sn_field('rewards', pubkey, False,
                        "Okay, I'll no longer send you block reward notifications for _{}_.")

            @commands.command()
            @dm_only
            async def soon(self, ctx, pubkey : str):
                """Enables "expires soon" notifications for a service node"""
                c = DiscordContext(ctx)
                c.set_sn_field('expires_soon', pubkey, True,
                        "Okay, I'll send you expiry notifications when _{}_ is close to expiry (48h, 24h, and 6h).")

            @commands.command()
            @dm_only
            async def nosoon(self, ctx, pubkey : str):
                """Disables "expires soon" notifications for a service node"""
                c = DiscordContext(ctx)
                c.set_sn_field('expires_soon', pubkey, False,
                        "Okay, I'll stop sending you notifications when _{}_ is close to expiry.")

            @commands.command()
            @dm_only
            async def sns(self, ctx):
                """Lists currently monitored service nodes"""
                DiscordContext(ctx).service_nodes()

            @commands.command()
            async def sn(self, ctx, pubkey : str):
                """Shows details of a service node; specify the index of the last service node list, or a full SN pubkey (if used in a channel, only a pubkey is allowed)"""
                pubkey = c.pubkey_from_arg(pubkey, send_errmsg=True)
                if pubkey:
                    DiscordContext(ctx).service_node(pubkey=pubkey)

            @commands.command(name='$')
            @dm_only
            async def sn_or_sns(self, ctx, pubkey : str=''):
                """A shortcut for either $sn or $sns: if given an argument it shows details of a service node; with no argument it lists monitored service nodes"""
                c = DiscordContext(ctx)
                if pubkey:
                    pubkey = c.pubkey_from_arg(pubkey, send_errmsg=True)
                    if pubkey:
                        c.service_node(pubkey=c.pubkey_from_arg(pubkey))
                else:
                    c.service_nodes()

        class WalletCommands(commands.Cog, name='Wallet-related commands'):
            @commands.command()
            @dm_only
            async def wallets(self, ctx):
                """List wallets the bot associates with your account"""
                DiscordContext(ctx).wallets_menu()

            @commands.command()
            @dm_only
            async def wallet(self, ctx, wallet : str=''):
                """Associates a wallet with your account (to be able to figure out which rewards/stakes are yours)"""
                await DiscordContext(ctx).ask_wallet(wallet)

            @commands.command()
            @dm_only
            async def nowallet(self, ctx, wallet : str):
                """Forgets a wallet associated with your account; pass the wallet (or wallet prefix) to forget"""
                DiscordContext(ctx).forget_wallet(wallet)

            @commands.command(aliases=['unmon'])
            @dm_only
            async def unmonitored(self, ctx):
                """Looks for any service nodes matching your wallet(s) (registered with `$wallet`) and starts monitoring them"""
                DiscordContext(ctx).find_unmonitored()

            @commands.command()
            @dm_only
            async def automon(self, ctx, enable : str=''):
                """Enables/disables automatic monitoring for new SNs to which you have contributed; specify "on" or "off" to enable/disable.  No argument shows the current setting."""
                c = DiscordContext(ctx)
                if enable:
                    if enable == 'on':
                        c.set_user_field('auto_monitor', True)
                    elif enable == 'off':
                        c.set_user_field('auto_monitor', False)
                    else:
                        c.send_reply("Invalid command; use one of `$automon on`, `$automon off`, or `$automon`")
                else:
                    c.send_reply("Auto-monitoring for new SNs is currently: " + self.b("enabled" if c.get_user_field('auto_monitor') else "disabled"))


        self.bot.add_cog(General())
        self.bot.add_cog(SNCommands())
        self.bot.add_cog(WalletCommands())



        @self.bot.event
        async def on_command_error(ctx, exc):
            c = DiscordContext(ctx)
            if c.is_dm() and isinstance(exc, (commands.MissingRequiredArgument, commands.errors.CommandNotFound)):
                c.send_reply("Invalid command `{}`: {}".format(ctx.message.content, exc))
            else:
                if isinstance(exc, commands.errors.CheckFailure) and not c.is_dm():
                    pass  # Ignore: this is a private command sent in public, it's supposed to fail.
                else:
                    print("A command error occured: command '{}' raised {}: {}".format(ctx.message.content, type(exc), exc))

        @self.bot.event
        async def on_message(message):
            channel = message.channel

            # Ignore self:
            if message.author == self.bot.user:
                return

            ctx = await self.bot.get_context(message)

            # First check whether this is a response to the bot waiting for input:
            global message_futures
            for i, f in enumerate(message_futures):
                if f[1](message):
                    f[0].set_result(message)
                    del message_futures[i]
                    return

            if ctx.command:
                await self.bot.invoke(ctx)
            else:
                c = DiscordContext(ctx)
                if c.is_dm() and not c.plain_input(message.content):
                    c.send_reply("Sorry, I didn't understand.  Try `$help` for a list of commands")

    def start(self):
        asyncio.ensure_future(self.bot.start(lokisnbot.config.DISCORD_TOKEN))

    def stop(self):
        asyncio.ensure_future(self.bot.logout())

    async def message_user(self, userid, message):
        user = self.bot.get_user(userid)
        await user.send(message)
        return True

    def try_message(self, chatid, message, append=None):
        """Send a message to the bot.  If the message gives a 'bot was blocked by the user' error then
        we delete the user's service_nodes (to stop generating more messages)."""
        if append:
            message += '\n' + append
        future = asyncio.run_coroutine_threadsafe(self.message_user(chatid, message), self.loop)
        try:
            future.result()
        except Exception as e:
            print("Sending to user {} failed: {}".format(chat_id, e))
            return False
        return True

    def sn_update_extra(self, sn):
        return { 'append': 'https://{0}/service_node/{1}'.format(explorer(sn.testnet), sn['pubkey']) }

    def ready(self):
        return self.bot.user is not None
