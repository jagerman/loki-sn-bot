
import time

import lokisnbot
from . import pgsql
from .constants import *

def lsr(h, testnet=False):
    if testnet:
        return 100
    elif h >= 235987:
        return 15000 + 24721 * 2**((101250-h)/129600.)
    else:
        return 10000 + 35000 * 2**((101250-h)/129600.)

def reward(h):
    return 14 + 50 * 2**(-h/64800)

class ServiceNode:
    _data = None
    _state = None
    testnet = False
    def __init__(self, data=None, snid=None, pubkey=None, uid=None):
        """
        Constructs a ServiceNode object.  Can take a data dict (which should contain at least all
        the fields fetched from a row of the service_nodes table), or a (pubkey or snid) + uid pair
        to do the query during construction.
        """
        if data:
            if 'pubkey' not in data:
                raise RuntimeError("Given service node data is invalid")
            self._data = dict(data)
        elif uid and (snid or pubkey):
            cur = pgsql.dict_cursor()
            key = 'id' if snid else 'pubkey'
            cur.execute('SELECT * FROM service_nodes WHERE ' + key + ' = %s AND uid = %s', (snid or pubkey, uid))
            self._data = cur.fetchone()
            if not self._data:
                raise ValueError("Given SN " + key + " is unknown/invalid")
        else:
            raise RuntimeError("Invalid arguments: either 'data' or 'pubkey'/'uid' arguments must be supplied")

        try:
            self._state = lokisnbot.sn_states[self._data['pubkey']]
        except KeyError:
            try:
                self._state = lokisnbot.testnet_sn_states[self._data['pubkey']]
                self.testnet = True
            except KeyError:
                self._state = None

        if all(x in self._data for x in ('testnet', 'id', 'uid')) and self._state and self.testnet != self._data['testnet']:
            pgsql.cursor().execute("UPDATE service_nodes SET testnet = %s WHERE id = %s AND uid = %s",
                    (self.testnet, self._data['id'], self._data['uid']))


    def __getitem__(self, key):
        return self._data[key]


    def active(self):
        """Returns true if this is a known SN on either mainnet or testnet"""
        return self._state is not None

    def staked(self):
        """Returns true if this SN is fully staked and active"""
        return self.active() and self._state['total_contributed'] >= self._state['staking_requirement']


    def state(self, key):
        return self._state[key] if self._state and key in self._state else None


    def update(self, **kwargs):
        cur = pgsql.cursor()
        sets, vals = [], []
        for k, v in kwargs.items():
            sets.append(k + ' = %s')
            vals.append(v)
        vals += (self._data['id'], self._data['uid'])
        cur.execute("UPDATE service_nodes SET " + ", ".join(sets) + " WHERE id = %s AND uid = %s", tuple(vals))

        self._data.update(kwargs)

    def shortpub(self):
        return self._data['pubkey'][0:6] + '…' + self._data['pubkey'][-3:]


    def alias(self):
        """Returns sn['alias'], if set, otherwise a ellipsized version of the pubkey"""
        return self._data['alias'] or self.shortpub()


    def operator_fee(self):
        """Returns the operator fee as a portion (NOT a percentage), e.g. returns 0.02 for a 2% fee.
        Returns None if this is not an active SN.  Note that solo nodes have a fee of 100%"""
        return self._state['portions_for_operator'] / 18446744073709551612. if self.active() else None


    def proof_age(self):
        lup = None
        if 'last_uptime_proof' in self._state:
            lup = self._state['last_uptime_proof']
        if not lup:
            return None
        return int(time.time() - lup)


    def format_proof_age(self):
        ago = self.proof_age()
        if ago is None:
            return '_No proof received_'
        seconds = ago % 60
        minutes = (ago // 60) % 60
        hours = (ago // 3600)
        return ('_No proof received_' if ago is None else
                (   '{}h{:02d}m{:02d}s'.format(hours, minutes, seconds) if hours else
                    '{}m{:02d}s'.format(minutes, seconds) if minutes else
                    '{}s'.format(seconds)
                    ) + ' ago' + (' ⚠' if ago >= PROOF_AGE_WARNING else '')
                )

    def moon_symbol(self, pct=None):
        if pct is None:
            pct = 0
            if self._state:
                pct = self._state['total_contributed'] / self._state['staking_requirement'] * 100
        return '🌑' if pct < 26 else '🌒' if pct < 50 else '🌓' if pct < 75 else '🌔' if pct < 100 else '🌕'


    def expiry_block(self):
        """Returns the block when this SN expires, or None if it isn't registered or doesn't expire"""
        if not self.active():  # FIXME: infinite stake
            return None
        return self._state['registration_height'] + (TESTNET_STAKE_BLOCKS if self.testnet else STAKE_BLOCKS)


    def expires_in(self):
        """Returns the estimate of the time until the stake expires, in seconds.  Returns None if
        the SN is not registered or if the SN uses infinite staking (once supported)."""
        if not self.active():
            return None
        elif self.testnet:
            height = lokisnbot.testnet_network_info['height']
            stake_blocks = TESTNET_STAKE_BLOCKS
        elif self._data:
            height = lokisnbot.network_info['height']
            stake_blocks = STAKE_BLOCKS
        return (self._state['registration_height'] + stake_blocks - height + 1) * AVERAGE_BLOCK_SECONDS


    def expires_soon(self):
        return self.expires_in() < 3600 * max(
                lokisnbot.config.TESTNET_EXPIRY_THRESHOLDS if self.testnet else lokisnbot.config.EXPIRY_THRESHOLDS)


    def status_icon(self):
        status_icon, prefix = '🛑', ''
        if not self.active():
            return status_icon
        elif self.testnet:
            height = lokisnbot.testnet_network_info['height']
            stake_blocks = TESTNET_STAKE_BLOCKS
            prefix = '🚧'
        else:
            height = lokisnbot.network_info['height']
            stake_blocks = STAKE_BLOCKS

        proof_age = int(time.time() - self._state['last_uptime_proof'])
        if proof_age >= PROOF_AGE_WARNING:
            status_icon = '⚠'
        elif self._state['total_contributed'] < self._state['staking_requirement']:
            status_icon = self.moon_symbol()
        elif self.expires_soon():
            status_icon = '⏱'
        else:
            status_icon = '💚'

        return prefix + status_icon

